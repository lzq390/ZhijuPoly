"""Fail-closed submission journal. This module deliberately uses only stdlib.

Initialize explicitly once with ``SubmissionJournal.initialize(path)``. Never
delete or recreate a lost journal: doing so destroys the deduplication history.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4


class JournalError(Exception):
    pass


class DraftError(Exception):
    """A user-visible conflict; unlike I/O errors this never poisons the journal."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SubmissionJournal:
    @staticmethod
    def initialize(path: Path) -> None:
        path = Path(path)
        if path.parent.stat().st_mode & 0o077:
            raise JournalError("Journal directory must be private (0700)")
        identity_path = path.with_name(path.name + ".identity")
        if identity_path.exists():
            raise JournalError("Journal already initialized; preserve its history")
        identity = uuid4().hex
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        # A failed initialization intentionally leaves an unusable file.
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE metadata (identity TEXT NOT NULL, backend_url TEXT, account_uuid TEXT)")
            db.execute("INSERT INTO metadata(identity) VALUES (?)", (identity,))
            db.execute("""CREATE TABLE submissions (
                client_id TEXT NOT NULL, account_uuid TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                body_hash TEXT NOT NULL, state TEXT NOT NULL,
                job_id TEXT, job_status TEXT, error_code TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(client_id,account_uuid,idempotency_key))""")
        descriptor = os.open(identity_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(identity)
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def __init__(self, path: Path, *, max_entries: int = 10000,
                 backend_url: str | None = None, account_uuid: str | None = None):
        self.path = Path(path)
        if max_entries < 1:
            raise ValueError("Journal capacity must be positive")
        self.max_entries = min(max_entries, 10000)
        self.failed = False
        self.lock_fd: int | None = None
        try:
            if self.path.parent.stat().st_mode & 0o077:
                raise JournalError("Journal directory must be private")
            self.file_identity = self._file_identity(self.path)
            self.identity_path = self.path.with_name(self.path.name + ".identity")
            self._file_identity(self.identity_path)
            self.identity = self.identity_path.read_text()
            self.lock_fd = os.open(self.path.with_name(self.path.name + ".lock"),
                                   os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            self.lock_identity = self._file_identity(self.path.with_name(self.path.name + ".lock"))
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self._transaction() as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise JournalError("Journal integrity check failed")
                if bool(backend_url) != bool(account_uuid):
                    raise JournalError("Journal backend and account must be bound together")
                if backend_url is not None:
                    target = db.execute("SELECT backend_url,account_uuid FROM metadata").fetchone()
                    if target[0] is None and target[1] is None:
                        db.execute("UPDATE metadata SET backend_url=?,account_uuid=?", (backend_url, account_uuid))
                    elif tuple(target) != (backend_url, account_uuid):
                        raise JournalError("Journal cannot change backend or account")
                # A preceding process could have sent its POST before dying.
                db.execute("UPDATE submissions SET state='outcome_unknown',error_code='SUBMISSION_OUTCOME_UNKNOWN',updated_at=? WHERE state='dispatching'", (utc_now(),))
                # Additive migration preserves every historical submission and
                # the journal identity; never replace or reinitialize this DB.
                db.execute("""CREATE TABLE IF NOT EXISTS generation_drafts (
                    draft_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, account_uuid TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL,
                    payload TEXT NOT NULL, job_id TEXT, error_code TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        except Exception:
            self.close()
            raise JournalError("Submission journal unavailable; preserve it for operator recovery") from None

    @staticmethod
    def _file_identity(path: Path) -> tuple[int, int]:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid() or info.st_nlink != 1):
            raise JournalError("Journal files must be private regular files")
        return info.st_dev, info.st_ino

    @contextmanager
    def _transaction(self):
        db = None
        try:
            if self.failed or self._file_identity(self.path) != self.file_identity:
                raise JournalError("Submission journal was removed or replaced")
            if self._file_identity(self.path.with_name(self.path.name + ".lock")) != self.lock_identity:
                raise JournalError("Journal process lock was removed or replaced")
            self._file_identity(self.identity_path)
            if self.identity_path.read_text() != self.identity:
                raise JournalError("Submission journal identity changed")
            db = sqlite3.connect(f"file:{quote(str(self.path), safe='/')}?mode=rw", uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            identity = db.execute("SELECT identity FROM metadata").fetchall()
            if len(identity) != 1 or identity[0][0] != self.identity:
                raise JournalError("Submission journal identity mismatch")
            yield db
            db.commit()
        except DraftError:
            if db is not None:
                db.rollback()
            raise
        except Exception:
            self.failed = True
            if db is not None:
                db.rollback()
            raise JournalError("Submission journal unavailable; submissions disabled") from None
        finally:
            if db is not None:
                db.close()

    def begin(self, client_id: str, account_uuid: str, key: str, body: dict) -> tuple[dict, bool]:
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        with self._transaction() as db:
            record = db.execute("SELECT * FROM submissions WHERE client_id=? AND account_uuid=? AND idempotency_key=?",
                                (client_id, account_uuid, key)).fetchone()
            if record is not None:
                item = dict(record)
                item["conflict"] = item["body_hash"] != digest
                return item, False
            if db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] >= self.max_entries:
                return {"state": "capacity"}, False
            now = utc_now()
            db.execute("INSERT INTO submissions(client_id,account_uuid,idempotency_key,body_hash,state,created_at,updated_at) VALUES(?,?,?,?,'dispatching',?,?)",
                       (client_id, account_uuid, key, digest, now, now))
            return {"state": "dispatching"}, True

    def finish(self, client_id: str, account_uuid: str, key: str, *, state: str,
               job_id: str | None = None, job_status: str | None = None, error_code: str | None = None) -> None:
        if state not in {"accepted", "rejected", "outcome_unknown"}:
            raise ValueError("Invalid journal transition")
        with self._transaction() as db:
            cursor = db.execute("UPDATE submissions SET state=?,job_id=?,job_status=?,error_code=?,updated_at=? WHERE client_id=? AND account_uuid=? AND idempotency_key=? AND state='dispatching'",
                                (state, job_id, job_status, error_code, utc_now(), client_id, account_uuid, key))
            if cursor.rowcount != 1:
                raise JournalError("Invalid journal transition")

    def owns_job(self, client_id: str, account_uuid: str, job_id: str) -> bool:
        with self._transaction() as db:
            return db.execute("SELECT 1 FROM submissions WHERE client_id=? AND account_uuid=? AND job_id=? AND state='accepted' LIMIT 1",
                              (client_id, account_uuid, job_id)).fetchone() is not None

    def submission_record(self, client_id: str, account_uuid: str, key: str) -> dict | None:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM submissions WHERE client_id=? AND account_uuid=? AND idempotency_key=?",
                             (client_id, account_uuid, key)).fetchone()
            return dict(row) if row is not None else None

    @staticmethod
    def _draft_record(row) -> dict:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _find_draft(db, client_id: str, account_uuid: str, conversation_id: str, draft_id: str) -> dict:
        row = db.execute("SELECT * FROM generation_drafts WHERE draft_id=? AND client_id=? AND account_uuid=? AND conversation_id=?",
                         (draft_id, client_id, account_uuid, conversation_id)).fetchone()
        if row is None:
            raise DraftError("DRAFT_NOT_FOUND", "No generation draft belongs to this client and conversation.")
        return SubmissionJournal._draft_record(row)

    @staticmethod
    def _check_revision(item: dict, revision: int) -> None:
        if item["revision"] != revision:
            raise DraftError("DRAFT_REVISION_CONFLICT", "The draft changed. Refresh its latest values before confirming or editing.")

    def create_draft(self, client_id: str, account_uuid: str, conversation_id: str, payload: dict) -> dict:
        with self._transaction() as db:
            if db.execute("SELECT COUNT(*) FROM generation_drafts").fetchone()[0] >= self.max_entries:
                raise DraftError("DRAFT_CAPACITY_LIMITED", "Draft history is full; an operator must preserve and migrate it.")
            draft_id, now = "ptd_" + uuid4().hex, utc_now()
            db.execute("INSERT INTO generation_drafts(draft_id,client_id,account_uuid,conversation_id,revision,state,payload,created_at,updated_at) VALUES(?,?,?,?,1,'editing',?,?,?)",
                       (draft_id, client_id, account_uuid, conversation_id,
                        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False), now, now))
            return self._find_draft(db, client_id, account_uuid, conversation_id, draft_id)

    def read_draft(self, client_id: str, account_uuid: str, conversation_id: str, draft_id: str) -> dict:
        with self._transaction() as db:
            return self._find_draft(db, client_id, account_uuid, conversation_id, draft_id)

    def update_draft(self, client_id: str, account_uuid: str, conversation_id: str, draft_id: str,
                     revision: int, payload: dict) -> dict:
        with self._transaction() as db:
            item = self._find_draft(db, client_id, account_uuid, conversation_id, draft_id)
            self._check_revision(item, revision)
            if item["state"] != "editing":
                raise DraftError("DRAFT_LOCKED", "Only an unconfirmed draft can be edited.")
            db.execute("UPDATE generation_drafts SET revision=revision+1,payload=?,updated_at=? WHERE draft_id=?",
                       (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False), utc_now(), draft_id))
            return self._find_draft(db, client_id, account_uuid, conversation_id, draft_id)

    def confirm_draft(self, client_id: str, account_uuid: str, conversation_id: str, draft_id: str,
                      revision: int) -> dict:
        with self._transaction() as db:
            item = self._find_draft(db, client_id, account_uuid, conversation_id, draft_id)
            self._check_revision(item, revision)
            if item["state"] == "cancelled":
                raise DraftError("DRAFT_CANCELLED", "This generation draft was cancelled without creating a task.")
            if item["state"] == "editing":
                db.execute("UPDATE generation_drafts SET state='confirmed',updated_at=? WHERE draft_id=?", (utc_now(), draft_id))
            return self._find_draft(db, client_id, account_uuid, conversation_id, draft_id)

    def cancel_draft(self, client_id: str, account_uuid: str, conversation_id: str, draft_id: str,
                     revision: int) -> dict:
        with self._transaction() as db:
            item = self._find_draft(db, client_id, account_uuid, conversation_id, draft_id)
            self._check_revision(item, revision)
            if item["state"] not in {"editing", "cancelled"}:
                raise DraftError("DRAFT_LOCKED", "A confirmed generation cannot be cancelled through the draft form.")
            db.execute("UPDATE generation_drafts SET state='cancelled',updated_at=? WHERE draft_id=?", (utc_now(), draft_id))
            return self._find_draft(db, client_id, account_uuid, conversation_id, draft_id)

    def finish_draft(self, client_id: str, account_uuid: str, conversation_id: str, draft_id: str,
                     *, state: str, job_id: str | None = None, error_code: str | None = None) -> dict:
        if state not in {"submitted", "submission_unknown", "rejected"}:
            raise ValueError("Invalid draft submission state")
        with self._transaction() as db:
            item = self._find_draft(db, client_id, account_uuid, conversation_id, draft_id)
            if item["state"] not in {"confirmed", "submitted", "submission_unknown", "rejected"}:
                raise DraftError("DRAFT_LOCKED", "An unconfirmed draft cannot record a submission.")
            db.execute("UPDATE generation_drafts SET state=?,job_id=?,error_code=?,updated_at=? WHERE draft_id=?",
                       (state, job_id, error_code, utc_now(), draft_id))
            return self._find_draft(db, client_id, account_uuid, conversation_id, draft_id)

    def close(self) -> None:
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

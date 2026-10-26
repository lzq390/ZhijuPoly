#!/usr/bin/env python3
"""Plan/run an offline backup drill on this task's dedicated private PostgreSQL.

plan writes metadata only: no DB command, lock, service action or engine. run
requires both dedicated API and Worker stopped and all external execution clean.
Its one-hour temporary credential exists only in memory and is dropped after
restoring ownership; no password or DSN is printed or saved.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]
CONTAINER = "nexpoly-polymer-md-dev-pg-6f6a"
SOURCE_DATABASE = "nexpoly_polymer_md_dev"


def _parent_fd(path, *, create=False):
    """Walk dirfds without following links; never chmod an existing directory."""
    path = Path(os.path.abspath(path))
    if not path.name:
        raise ValueError("private metadata needs a filename")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parent.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("private metadata parent must be owned and owner-only")
        return path, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _private_file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ValueError("private backup metadata must be an owned unlinked regular file")


def _json_bytes(value):
    data = (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()
    if len(data) > 1024 * 1024:
        raise ValueError("private backup metadata is too large")
    return data


def _write_all(descriptor, data):
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("incomplete private metadata write")
        view = view[written:]


def _save_new_at(parent, name, data):
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
    try:
        _private_file(os.fstat(descriptor))
        _write_all(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(parent)


def private_json(path):
    path, parent = _parent_fd(path)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            _private_file(os.fstat(descriptor))
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                data = source.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("private backup metadata is too large")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError("private backup metadata must be an object")
            return result
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def save(path, value):
    """Create a new private JSON file exclusively; preserve every existing path."""
    data = _json_bytes(value)
    path, parent = _parent_fd(path, create=True)
    try:
        _save_new_at(parent, path.name, data)
    finally:
        os.close(parent)


class EvidenceJournal:
    """Reserve the final JSON inode and preserve immutable write-ahead receipts.

    Updates use the reserved fd, never a predictable temporary filename/replace.
    If interrupted during final JSON refresh, the last complete receipt remains
    under the private history directory. No receipt or foreign file is deleted.
    """
    def __init__(self, path):
        self.path, self.parent = _parent_fd(path, create=True)
        self.fd = self.history_fd = None
        try:
            self.fd = os.open(self.path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.parent)
            self.identity = os.fstat(self.fd)
            _private_file(self.identity)
            self.history_name = "." + self.path.name + ".records-" + uuid4().hex
            os.mkdir(self.history_name, 0o700, dir_fd=self.parent)
            self.history_fd = os.open(self.history_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.parent)
            self.sequence = 0
            os.fsync(self.parent)
        except BaseException:
            self.close()
            raise

    def _binding(self):
        _, current_parent = _parent_fd(self.path)
        try:
            original = os.fstat(self.parent)
            now = os.fstat(current_parent)
            if (original.st_dev, original.st_ino) != (now.st_dev, now.st_ino):
                raise ValueError("evidence parent identity changed")
        finally:
            os.close(current_parent)
        current = os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)
        _private_file(current)
        if (current.st_dev, current.st_ino) != (self.identity.st_dev, self.identity.st_ino):
            raise ValueError("evidence file identity changed")

    def publish(self, value):
        self._binding()
        value = {**value, "evidence_history": str(self.path.parent / self.history_name)}
        data = _json_bytes(value)
        _save_new_at(self.history_fd, f"{self.sequence:06d}.json", data)
        self.sequence += 1
        self._binding()
        os.lseek(self.fd, 0, os.SEEK_SET)
        _write_all(self.fd, data)
        os.ftruncate(self.fd, len(data))
        os.fsync(self.fd)
        os.fsync(self.parent)

    def close(self):
        for name in ("history_fd", "fd", "parent"):
            descriptor = getattr(self, name, None)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)


def formal_journals(job_root):
    """Reject live/unconfirmed jobs using only the already reviewed journal API."""
    from workers.polymer_md_worker.app.journal import read_json, safe_file
    records = []
    for job in sorted(job_root.iterdir()):
        if job.name.startswith("."):
            continue
        if job.is_symlink() or not job.is_dir():
            raise RuntimeError("unsafe formal job directory")
        for attempt in sorted(job.iterdir()):
            if attempt.is_symlink():
                raise RuntimeError("unsafe formal attempt directory")
            if not attempt.is_dir():
                continue
            path = attempt / "journal.json"
            if path.is_symlink():
                raise RuntimeError("unsafe formal journal")
            if not path.exists():
                continue
            record = read_json(safe_file(job_root, path.relative_to(job_root).as_posix()))
            snapshot = record["snapshot"]
            if (snapshot["status"] not in {"completed", "failed", "cancelled"}
                    or snapshot.get("cleanup_confirmed") is not True or record.get("running_scope")):
                raise RuntimeError("every formal job must be terminal with confirmed scope cleanup")
            records.append(path)
    return records




def new_output(path, active_roots):
    if not path.is_absolute():
        raise ValueError("backup output must be absolute")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError("backup output must not have a symbolic-link ancestor")
    if path.exists() or any(path.resolve().is_relative_to(root.resolve()) for root in active_roots):
        raise ValueError("backup output must be new and outside active roots")


def current_receipts():
    root = ROOT / ".runtime/polymer-md"
    worker = private_json(root / "worker-runtime.json")
    backend = private_json(root / "backend-runtime.json")
    return worker, backend


def verify_plan_binding(document, evidence_path):
    worker, backend = current_receipts()
    bindings = {"runtime": worker["runtime"], "job_root": worker["job_root"],
                "runtime_fingerprint": worker["runtime_fingerprint"],
                "api_unit": backend["unit"], "worker_unit": worker["unit"]}
    if any(document.get(key) != value for key, value in bindings.items()):
        raise ValueError("dedicated environment changed after the backup plan")
    identity = private_json(Path(backend["runtime"]) / "backend-private.json")
    if (identity["database"] != SOURCE_DATABASE or
            hashlib.sha256(identity["marker"].encode()).hexdigest() != document["source_marker_sha256"]):
        raise ValueError("dedicated environment changed after the backup plan")
    roots = [Path(worker["runtime"]), Path(worker["job_root"]), Path(backend["runtime"]), ROOT / ".runtime/polymer-md"]
    output = Path(document["output"])
    new_output(output, roots)
    new_output(evidence_path, roots)
    if evidence_path == output or evidence_path.is_relative_to(output) or output.is_relative_to(evidence_path):
        raise ValueError("backup evidence and output must be separate")


def plan(output):
    receipt_root = ROOT / ".runtime/polymer-md"
    worker = private_json(receipt_root / "worker-runtime.json")
    backend = private_json(receipt_root / "backend-runtime.json")
    runtime, job_root = Path(worker["runtime"]), Path(worker["job_root"])
    auth = Path(backend["runtime"])
    identity = private_json(auth / "backend-private.json")
    if identity["database"] != SOURCE_DATABASE:
        raise ValueError("backup refuses another development database")
    new_output(output, [job_root, runtime, auth, receipt_root])
    nonce = uuid4().hex[:12]
    return {"version": 1, "purpose": "private_polymer_md_offline_backup_drill",
        "created_at": datetime.now(timezone.utc).isoformat(), "container": CONTAINER,
        "source_database": SOURCE_DATABASE, "source_marker_sha256": hashlib.sha256(identity["marker"].encode()).hexdigest(),
        "restore_database": "polymer_md_restore_current_" + nonce,
        "temporary_role": "polymer_md_backup_operator_" + nonce, "temporary_credential_persisted": False,
        "runtime": str(runtime), "runtime_fingerprint": worker["runtime_fingerprint"], "job_root": str(job_root),
        "api_unit": backend["unit"], "worker_unit": worker["unit"], "output": str(output.absolute()),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "plan_executes_database_or_locks": False, "scientific_acceptance": False}


def psql(database, statement, *, result=False):
    # SQL is supplied on stdin; even the generated password never enters argv.
    process = subprocess.run(["docker", "exec", "-i", "-u", "postgres", CONTAINER,
        "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", database],
        input=statement, text=True, capture_output=True)
    if process.returncode:
        raise RuntimeError("private local PostgreSQL maintenance failed")
    return json.loads(process.stdout) if result and process.stdout.strip() else None


def inactive(unit):
    if not re.fullmatch(r"nexpoly-pmd-[a-z0-9-]+\.service", unit):
        raise ValueError("backup only checks dedicated polymer MD units")
    status = subprocess.run(["systemctl", "--user", "show", unit, "--property=ActiveState", "--value"],
                            text=True, capture_output=True, check=True).stdout.strip()
    if status not in {"inactive", "failed"}:
        raise ValueError("stop the dedicated API and Worker before offline backup")


def run(document, evidence_path):
    if document.get("version") != 1 or document.get("container") != CONTAINER or document.get("source_database") != SOURCE_DATABASE:
        raise ValueError("backup plan does not belong to this dedicated environment")
    restore, role = document["restore_database"], document["temporary_role"]
    if not re.fullmatch(r"polymer_md_restore_current_[0-9a-f]{12}", restore) or not re.fullmatch(r"polymer_md_backup_operator_[0-9a-f]{12}", role):
        raise ValueError("invalid generated backup identity")
    output, runtime, job_root = Path(document["output"]), Path(document["runtime"]), Path(document["job_root"])
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != document.get("driver_sha256"):
        raise ValueError("backup driver changed after planning")
    verify_plan_binding(document, evidence_path)
    for unit in (document["api_unit"], document["worker_unit"]):
        inactive(unit)
    manifest = private_json(runtime / "manifest.json")
    if manifest["runtime_fingerprint"] != document["runtime_fingerprint"]:
        raise ValueError("runtime changed after the backup plan")
    from workers.polymer_md_worker.app.external_execution import assert_no_external_execution
    from workers.polymer_md_worker.app.journal import RootLock
    guard = RootLock(job_root)
    guard.acquire()
    journal = None
    record = {"version": 1, "status": "preparing", "source_database": SOURCE_DATABASE,
              "restore_database": restore, "temporary_role": role, "job_root": str(job_root),
              "runtime_fingerprint": document["runtime_fingerprint"], "credential_persisted": False,
              "drop_owned_used": False, "scientific_acceptance": False,
              "role_creation_attempted": False, "owned_temporary_role_created": False,
              "restore_database_creation_attempted": False, "restore_database_created": False,
              "role_cleanup_confirmed": False}
    try:
        verify_plan_binding(document, evidence_path)
        for unit in (document["api_unit"], document["worker_unit"]):
            inactive(unit)
        current_manifest = private_json(runtime / "manifest.json")
        if current_manifest["runtime_fingerprint"] != document["runtime_fingerprint"]:
            raise ValueError("runtime changed after the backup plan")
        _, output_parent = _parent_fd(output)
        os.close(output_parent)
        assert_no_external_execution(job_root)
        record["terminal_journals_checked"] = len(formal_journals(job_root))
        journal = EvidenceJournal(evidence_path)
        journal.publish(record)
        marker = psql("postgres", "SELECT to_json(shobj_description(oid,'pg_database')) FROM pg_database WHERE datname='nexpoly_polymer_md_dev';", result=True)
        if hashlib.sha256((marker or "").encode()).hexdigest() != document["source_marker_sha256"]:
            raise ValueError("private database ownership marker changed")
        exists = psql("postgres", f"SELECT to_json(EXISTS(SELECT FROM pg_roles WHERE rolname='{role}') OR EXISTS(SELECT FROM pg_database WHERE datname='{restore}'));", result=True)
        if exists:
            raise ValueError("do not adopt an existing restore database or temporary role")
        password = secrets.token_hex(32)
        expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        record["role_creation_attempted"] = True
        journal.publish(record)
        psql("postgres", f'CREATE ROLE "{role}" LOGIN SUPERUSER PASSWORD \'{password}\' VALID UNTIL \'{expiry}\';')
        record["owned_temporary_role_created"] = True
        record["restore_database_creation_attempted"] = True
        journal.publish(record)
        psql("postgres", f'CREATE DATABASE "{restore}" TEMPLATE template0 OWNER postgres;')
        record["restore_database_created"] = True
        journal.publish(record)
        from psycopg.conninfo import make_conninfo
        from app.auth.backup import backup_and_verify
        source_dsn = make_conninfo(host="127.0.0.1", port=32952, dbname=SOURCE_DATABASE, user=role, password=password)
        restore_dsn = make_conninfo(host="127.0.0.1", port=32952, dbname=restore, user=role, password=password)
        receipt = backup_and_verify(source_dsn, restore_dsn, output, asset_roots={"polymer_md": job_root})
        record.update(status="restore_verified", receipt=str(receipt), restore_verified=True)
    except Exception as error:
        record.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        try:
            try:
                if record["owned_temporary_role_created"]:
                    # Disable the confirmed-owned temporary login before cleanup.
                    psql("postgres", f'ALTER ROLE "{role}" NOLOGIN NOSUPERUSER;')
                    record["temporary_role_disabled"] = True
                    for database in (SOURCE_DATABASE, restore) if record["restore_database_created"] else (SOURCE_DATABASE,):
                        psql(database, f'REASSIGN OWNED BY "{role}" TO postgres;')
                    remaining = psql("postgres", f"SELECT count(*) FROM pg_shdepend WHERE refclassid='pg_authid'::regclass AND refobjid=(SELECT oid FROM pg_roles WHERE rolname='{role}');", result=True)
                    if remaining:
                        record["remaining_dependencies"] = remaining
                        raise RuntimeError("temporary backup role dependencies need explicit review")
                    psql("postgres", f'DROP ROLE "{role}";')
                    record["role_cleanup_confirmed"] = True
                elif record["role_creation_attempted"]:
                    # An uncertain CREATE must never be reported as cleaned, nor
                    # used as a reason to adopt/drop an unverified same-name role.
                    record["role_creation_uncertain"] = True
                else:
                    record["role_cleanup_confirmed"] = True
                if record["restore_database_creation_attempted"] and not record["restore_database_created"]:
                    record["restore_database_creation_uncertain"] = True
            except Exception as cleanup_error:
                record["role_cleanup_confirmed"] = False
                record["cleanup_error_type"] = type(cleanup_error).__name__
                if record["status"] == "restore_verified":
                    record["status"] = "cleanup_unconfirmed"
                raise
            finally:
                if journal is not None:
                    journal.publish(record)
        finally:
            try:
                if journal is not None:
                    journal.close()
            finally:
                guard.release()
    return record




def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "run"))
    parser.add_argument("--plan-file", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.action == "plan":
        if args.output is None or args.plan_file.exists():
            parser.error("plan requires a new --plan-file and new --output")
        document = plan(args.output)
        save(args.plan_file, document)
        print(json.dumps({"planned": True, "database_or_lock_action": False, "plan_file": str(args.plan_file)}))
    else:
        if args.evidence is None or args.evidence.exists():
            parser.error("run requires a new --evidence path")
        result = run(private_json(args.plan_file), args.evidence)
        print(json.dumps({"restore_verified": result.get("restore_verified", False),
                          "role_cleanup_confirmed": result.get("role_cleanup_confirmed", False)}))
    return 0


def main():
    previous_umask = os.umask(0o077)
    try:
        return _main()
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"completed": False, "error_type": type(error).__name__, "review_private_evidence": True}))
        raise SystemExit(1)

#!/usr/bin/env python3
"""Publish an explicit scientific review without changing a sealed runtime.

This local operator command neither performs nor approves scientific analysis.
It requires an existing private decision document and verifies the referenced
evidence bytes. No service, database, engine or job journal is modified.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from uuid import NAMESPACE_URL, uuid4, uuid5

ROOT = Path(__file__).absolute().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]

MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_EVIDENCE_BYTES = 1024 * 1024 * 1024
MAX_TOTAL_EVIDENCE_BYTES = 2 * MAX_EVIDENCE_BYTES
REQUIRED_EVIDENCE_LABELS = {"raw_benchmark", "independent_recompute", "review_report"}


def safe_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("operator paths must be absolute without parent traversal")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("operator paths must not contain symlinks")
    return path


def private_stat(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("review and evidence must be owned private regular files")
    return info


def private_bytes(path: Path, maximum=MAX_DOCUMENT_BYTES) -> bytes:
    path = safe_path(path)
    before = private_stat(path)
    if before.st_size > maximum:
        raise ValueError("private document exceeds the operator document budget")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if len(data) > maximum or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise ValueError("private document changed while reading")
    return data


def json_document(path: Path) -> tuple[dict, str]:
    data = private_bytes(path)
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate decision JSON key")
            value[key] = item
        return value
    value = json.loads(data, object_pairs_hook=unique_pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite decision JSON")))
    if not isinstance(value, dict):
        raise ValueError("decision must be a JSON object")
    return value, hashlib.sha256(data).hexdigest()


def binding_from_manifest(path: Path) -> tuple[dict, dict, dict]:
    from app.routers.polymer_md import _scientific_protocol
    from app.services.polymer_md_acceptance_registry import canonical_sha256

    manifest, _ = json_document(path)
    unsigned = {key: value for key, value in manifest.items() if key != "runtime_fingerprint"}
    if canonical_sha256(unsigned) != manifest.get("runtime_fingerprint"):
        raise ValueError("sealed runtime fingerprint does not match its bytes")
    protocol = _scientific_protocol()
    binding = {key: manifest.get(key) for key in ("runtime_fingerprint", "source_sha", "patch_version")}
    binding["scientific_protocol_sha256"] = canonical_sha256(protocol)
    return binding, manifest, protocol


def verify_evidence(entries: list[dict]):
    total = 0
    if not REQUIRED_EVIDENCE_LABELS <= {item["label"] for item in entries}:
        raise ValueError("raw benchmark, independent recomputation and review report are required")
    for entry in entries:
        path = safe_path(entry["path"])
        before = private_stat(path)
        total += before.st_size
        if before.st_size != entry["size_bytes"] or before.st_size > MAX_EVIDENCE_BYTES or total > MAX_TOTAL_EVIDENCE_BYTES:
            raise ValueError("evidence size or total evidence budget does not match")
        digest = hashlib.sha256()
        observed = 0
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                observed += len(block)
                if observed > entry["size_bytes"] or observed > MAX_EVIDENCE_BYTES:
                    raise ValueError("evidence grew beyond its declared size or evidence budget")
                digest.update(block)
            after = os.fstat(stream.fileno())
        if observed != entry["size_bytes"] or digest.hexdigest() != entry["sha256"] or (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("evidence bytes changed or do not match the reviewed hash")


def prepare_approval(review_path: Path, manifest_path: Path) -> dict:
    from app.services.polymer_md_acceptance_registry import canonical_sha256, validate_receipt

    review, document_sha = json_document(review_path)
    expected = {"decision", "reviewer", "reviewed_at", "protocol_id", "binding", "scopes", "evidence"}
    if set(review) != expected or review["decision"] != "approved":
        raise ValueError("publish requires a complete existing explicit approved decision")
    binding, _, _ = binding_from_manifest(manifest_path)
    if review["binding"] != binding:
        raise ValueError("review does not bind this sealed runtime and scientific protocol")
    body = {"schema_version": 1, "kind": "approval",
        "receipt_id": uuid5(NAMESPACE_URL, "nexpoly-polymer-md-approval-v1:" + document_sha).hex,
        "protocol_id": review["protocol_id"], "binding": binding,
        "review": {"decision": "approved", "reviewer": review["reviewer"],
            "reviewed_at": review["reviewed_at"], "decision_sha256": document_sha},
        "scopes": review["scopes"], "evidence": review["evidence"]}
    body["receipt_sha256"] = canonical_sha256(body)
    body = validate_receipt(body)
    verify_evidence(body["evidence"])
    return body


def prepare_revocation(review_path: Path) -> dict:
    from app.services.polymer_md_acceptance_registry import canonical_sha256, validate_revocation

    review, document_sha = json_document(review_path)
    expected = {"decision", "reviewer", "reviewed_at", "receipt_id", "receipt_sha256"}
    if set(review) != expected or review["decision"] != "revoked":
        raise ValueError("revoke requires an existing explicit revoked decision")
    body = {"schema_version": 1, "kind": "revocation",
        "revocation_id": uuid5(NAMESPACE_URL, "nexpoly-polymer-md-revocation-v1:" + document_sha).hex,
        "receipt_id": review["receipt_id"], "receipt_sha256": review["receipt_sha256"],
        "review": {"decision": "revoked", "reviewer": review["reviewer"],
            "reviewed_at": review["reviewed_at"], "decision_sha256": document_sha}}
    body["revocation_sha256"] = canonical_sha256(body)
    return validate_revocation(body)


def private_directory(path: Path):
    path = safe_path(path)
    if not path.exists():
        missing, parent = [], path
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("registry directories must be owner private")
    return path


@contextmanager
def registry_lock(root: Path):
    private_directory(root)
    for name in ("receipts", "revocations"):
        private_directory(root / name)
    lock_path = root / ".registry.lock"
    safe_path(lock_path)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("registry publication lock is not private")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def document_bytes(document: dict) -> bytes:
    from app.services.polymer_md_acceptance_registry import MAX_FILE_BYTES

    data = (json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("review receipt exceeds the reader document budget")
    return data


def immutable_publish(root: Path, document: dict) -> Path:
    data = document_bytes(document)
    key, folder = ("receipt_id", "receipts") if document["kind"] == "approval" else ("revocation_id", "revocations")
    target = root / folder / (document[key] + ".json")
    temporary = root / folder / (".prepare-" + uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o400)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)  # Publication cannot overwrite an old decision.
        descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
        descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return target


def check_publication_budget(root: Path, document: dict):
    from app.routers.polymer_md import _scientific_protocol
    from app.services.polymer_md_acceptance_registry import acceptance_summary, MAX_EVENTS, MAX_TOTAL_BYTES

    data = document_bytes(document)
    if document["kind"] == "approval":
        binding = document["binding"]
    else:
        original, _ = json_document(root / "receipts" / (document["receipt_id"] + ".json"))
        binding = original["binding"]
    status = acceptance_summary(str(root), binding, _scientific_protocol())
    if status["registry_status"] == "invalid":
        raise ValueError("existing registry must validate before adding an event")
    events, total = 1, len(data)
    for name in ("receipts", "revocations"):
        folder = root / name
        if not folder.exists():
            continue
        for path in folder.iterdir():
            events += 1
            total += private_stat(path).st_size
            if events > MAX_EVENTS or total > MAX_TOTAL_BYTES:
                raise ValueError("publication would exceed the reader registry budget")


def publish(root: Path, document: dict, *, dry_run: bool = False) -> dict:
    from app.services.polymer_md_acceptance_registry import validate_receipt, validate_revocation

    root = safe_path(root)
    document = validate_receipt(document) if document.get("kind") == "approval" else validate_revocation(document)
    document_bytes(document)
    for path in (root, root / "receipts", root / "revocations"):
        safe_path(path)
        if path.exists():
            info = path.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("existing registry directories must be owner private")
    if document["kind"] == "approval":
        verify_evidence(document["evidence"])
    if document["kind"] == "revocation":
        original, _ = json_document(root / "receipts" / (document["receipt_id"] + ".json"))
        original = validate_receipt(original)
        if original["receipt_sha256"] != document["receipt_sha256"]:
            raise ValueError("revocation does not match the immutable original decision")
    check_publication_budget(root, document)
    if dry_run:
        return {"written": False, "dry_run": True, "kind": document["kind"]}
    with registry_lock(root):
        check_publication_budget(root, document)
        if document["kind"] == "approval":
            verify_evidence(document["evidence"])
        else:
            original, _ = json_document(root / "receipts" / (document["receipt_id"] + ".json"))
            if validate_receipt(original)["receipt_sha256"] != document["receipt_sha256"]:
                raise ValueError("original decision changed before publication")
        immutable_publish(root, document)
    return {"written": True, "kind": document["kind"],
        "record_id": document.get("receipt_id") if document["kind"] == "approval" else document["revocation_id"],
        "record_sha256": document.get("receipt_sha256") if document["kind"] == "approval" else document["revocation_sha256"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("publish", "revoke", "inspect"):
        command = sub.add_parser(action)
        command.add_argument("--registry", type=Path, required=True)
        if action != "revoke":
            command.add_argument("--manifest", type=Path, required=True)
        if action != "inspect":
            command.add_argument("--review", type=Path, required=True)
            command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "inspect":
        from app.services.polymer_md_acceptance_registry import acceptance_summary
        _, manifest, protocol = binding_from_manifest(args.manifest)
        print(json.dumps(acceptance_summary(str(safe_path(args.registry)), manifest, protocol), sort_keys=True))
    else:
        document = prepare_approval(args.review, args.manifest) if args.action == "publish" else prepare_revocation(args.review)
        print(json.dumps(publish(args.registry, document, dry_run=args.dry_run), sort_keys=True))
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"completed": False, "error_type": type(error).__name__, "review_private_inputs": True}))
        raise SystemExit(1)

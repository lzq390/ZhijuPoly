from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifacts import (
    atomic_write_bytes,
    atomic_write_json,
    describe_artifact,
    ensure_private_directory,
)
from .config import (
    PRODUCTION_REPO_ROOT,
    validate_dev_runtime_path,
    validate_private_dev_runtime_root,
)
from .schemas import (
    MAX_ENQUEUE_SEQUENCE,
    JobJournalV2,
    JobSnapshot,
    JobSubmitRequest,
    LegacyJobSnapshotV1,
    StructuredError,
    default_job_timings,
)


LEGACY_SEQUENCE_NAMESPACE_START = 1 << 62
MAX_JOURNAL_BYTES = 16 * 1024 * 1024


class JournalUpgradeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlannedUpgrade:
    path: Path
    original_bytes: bytes
    journal: JobJournalV2


def _dev_root_for_job(
    job_root: Path,
    dev_runtime_root: Path | None = None,
) -> Path:
    configured = os.getenv("MONOMER_DFT_DEV_RUNTIME_ROOT", "").strip()
    absolute_job_root = Path(os.path.abspath(os.path.normpath(job_root)))
    production_root = Path(os.path.abspath(PRODUCTION_REPO_ROOT))
    if (
        absolute_job_root == production_root
        or production_root in absolute_job_root.parents
    ):
        raise JournalUpgradeError(
            "MONOMER_DFT_JOB_ROOT must not reference the production repository"
        )
    if configured:
        selected = Path(configured)
    elif dev_runtime_root is not None:
        selected = Path(dev_runtime_root)
    else:
        selected = absolute_job_root.parent
        while not selected.exists() and selected != selected.parent:
            selected = selected.parent
    try:
        return validate_private_dev_runtime_root(selected)
    except ValueError as exc:
        raise JournalUpgradeError(str(exc)) from exc


def _validated_job_root(
    job_root: Path,
    dev_runtime_root: Path | None = None,
) -> tuple[Path, Path]:
    runtime_root = _dev_root_for_job(job_root, dev_runtime_root)
    try:
        root = validate_dev_runtime_path(
            "MONOMER_DFT_JOB_ROOT",
            Path(job_root),
            runtime_root=runtime_root,
            leaf_kind="directory",
        )
    except ValueError as exc:
        raise JournalUpgradeError(str(exc)) from exc
    return root, runtime_root


class JobRootLock:
    """Non-blocking process lock shared by the Worker and offline upgrader."""

    def __init__(
        self,
        job_root: Path,
        *,
        dev_runtime_root: Path | None = None,
    ) -> None:
        self.job_root, self.dev_runtime_root = _validated_job_root(
            job_root,
            dev_runtime_root,
        )
        self._descriptor: int | None = None

    def acquire(self) -> None:
        ensure_private_directory(self.job_root)
        lock_path = self.job_root / ".worker.lock"
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise JournalUpgradeError("the Worker lock path is unsafe") from exc
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            raise JournalUpgradeError(
                "the monomer DFT Worker must be stopped before journal inspection or upgrade"
            ) from exc
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def __enter__(self) -> "JobRootLock":
        self.acquire()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


def discover_journal_paths(job_root: Path) -> list[Path]:
    root, _ = _validated_job_root(job_root)
    if root.is_symlink() or not root.is_dir():
        raise JournalUpgradeError("job root must be a real directory")
    journals: list[Path] = []
    for job_directory in sorted(root.iterdir(), key=lambda item: item.name):
        if job_directory.name.startswith("."):
            continue
        if job_directory.is_symlink() or not job_directory.is_dir():
            raise JournalUpgradeError("unsafe entry below the job root")
        job_journals: list[Path] = []
        for attempt_directory in sorted(
            job_directory.iterdir(), key=lambda item: item.name
        ):
            if attempt_directory.name.startswith("."):
                continue
            if attempt_directory.is_symlink() or not attempt_directory.is_dir():
                raise JournalUpgradeError("unsafe attempt entry below a job")
            journal = attempt_directory / "journal.json"
            if journal.is_symlink():
                raise JournalUpgradeError("a journal path is a symbolic link")
            if journal.is_file():
                job_journals.append(journal)
        if len(job_journals) > 1:
            raise JournalUpgradeError("a job has multiple durable attempt journals")
        journals.extend(job_journals)
    return journals


def _read_journal(path: Path) -> tuple[bytes, dict[str, Any]]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise JournalUpgradeError("a journal could not be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size > MAX_JOURNAL_BYTES:
            raise JournalUpgradeError("a journal exceeds the 16 MiB safety limit")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(MAX_JOURNAL_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalUpgradeError("a journal is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise JournalUpgradeError("a journal root must be a JSON object")
    return content, value


def _validate_sequence_map(value: Mapping[str, Any] | None) -> dict[str, int]:
    if value is None:
        return {}
    result: dict[str, int] = {}
    for raw_job_id, raw_sequence in value.items():
        job_id = str(raw_job_id)
        if isinstance(raw_sequence, bool) or not isinstance(raw_sequence, int):
            raise JournalUpgradeError(
                "every mapped enqueue sequence must be an integer"
            )
        if not 1 <= raw_sequence <= MAX_ENQUEUE_SEQUENCE:
            raise JournalUpgradeError(
                "a mapped enqueue sequence is outside the V2 range"
            )
        result[job_id] = raw_sequence
    if len(set(result.values())) != len(result):
        raise JournalUpgradeError(
            "the sequence map contains duplicate enqueue sequences"
        )
    return result


def _local_legacy_sequence(job_id: str, used: set[int]) -> int:
    digest = hashlib.sha256(job_id.encode("utf-8")).digest()
    span = MAX_ENQUEUE_SEQUENCE - LEGACY_SEQUENCE_NAMESPACE_START + 1
    candidate = LEGACY_SEQUENCE_NAMESPACE_START + (
        int.from_bytes(digest[:8], "big") % span
    )
    for _ in range(span):
        if candidate not in used:
            return candidate
        candidate += 1
        if candidate > MAX_ENQUEUE_SEQUENCE:
            candidate = LEGACY_SEQUENCE_NAMESPACE_START
    raise JournalUpgradeError("the legacy enqueue sequence namespace is exhausted")


def _migrate_legacy_snapshot(
    snapshot: LegacyJobSnapshotV1,
    *,
    journal_path: Path,
    mapped_sequence: int | None,
    used_sequences: set[int],
    recover_active: bool,
) -> JobJournalV2:
    if mapped_sequence is not None:
        sequence = mapped_sequence
        source = "legacy_mapping"
    else:
        sequence = _local_legacy_sequence(snapshot.job_id, used_sequences)
        source = "legacy_terminal_local"
    if sequence in used_sequences:
        raise JournalUpgradeError("duplicate enqueue sequence across durable journals")
    used_sequences.add(sequence)

    migrated_request = JobSubmitRequest.model_validate(
        {
            **snapshot.request.model_dump(mode="json"),
            "schema_version": 2,
            "enqueue_sequence": sequence,
        }
    )
    migrated_timings = default_job_timings()
    migrated_timings.update(snapshot.timings)
    migrated = JobSnapshot.model_validate(
        {
            **snapshot.model_dump(
                mode="python",
                exclude={"schema_version", "request", "timings"},
            ),
            "schema_version": 2,
            "enqueue_sequence": sequence,
            "request": migrated_request,
            "timings": migrated_timings,
        }
    )
    migrated.queue_position = None
    if migrated.status in {"running", "cancel_requested"}:
        if not recover_active:
            raise JournalUpgradeError(
                "active V1 journals require the explicit --recover-active mode"
            )
        migrated.status = "failed"
        migrated.finished_at = migrated.updated_at
        migrated.error = StructuredError(
            code="worker_restarted",
            message="The worker stopped while this calculation was running.",
            retryable=True,
        )
        migrated.artifacts = []
    elif migrated.status in {"pending", "queued"} and mapped_sequence is None:
        if not recover_active:
            raise JournalUpgradeError(
                "active V1 journals require the explicit --recover-active mode"
            )
        migrated.status = "failed"
        migrated.stage = "validating"
        migrated.finished_at = migrated.updated_at
        migrated.error = StructuredError(
            code="journal_upgrade_missing_enqueue_sequence",
            message=(
                "This legacy queued job had no authoritative FIFO sequence and "
                "was not resumed automatically."
            ),
            retryable=True,
        )
        migrated.artifacts = []
    elif migrated.status in {"failed", "cancelled"}:
        migrated.artifacts = []

    manifest = list(migrated.artifacts) if migrated.status == "completed" else []
    artifact_state = "available" if manifest else "none"
    delete_requested_at = None
    deleted_at = None
    if migrated.status == "completed" and not manifest:
        attempt_directory = journal_path.parent
        artifact_directory = attempt_directory / "artifacts"
        artifact_tombstone = attempt_directory / ".artifacts.deleting"
        bundle_path = attempt_directory / "artifact_bundle.zip"
        bundle_tombstone = attempt_directory / ".artifact_bundle.zip.deleting"
        for path in (
            artifact_directory,
            artifact_tombstone,
            bundle_path,
            bundle_tombstone,
        ):
            if path.is_symlink():
                raise JournalUpgradeError(
                    "a legacy completed job contains an unsafe artifact path"
                )
        if artifact_directory.exists() and artifact_tombstone.exists():
            raise JournalUpgradeError(
                "legacy artifact directory and deletion tombstone coexist"
            )
        if bundle_path.exists() and bundle_tombstone.exists():
            raise JournalUpgradeError(
                "legacy artifact bundle and deletion tombstone coexist"
            )
        for path in (bundle_path, bundle_tombstone):
            if path.exists() and not path.is_file():
                raise JournalUpgradeError(
                    "a legacy artifact bundle remnant is not a regular file"
                )
        source_directory = (
            artifact_directory if artifact_directory.exists() else artifact_tombstone
        )
        remnants_exist = any(
            path.exists()
            for path in (
                artifact_directory,
                artifact_tombstone,
                bundle_path,
                bundle_tombstone,
            )
        )
        if remnants_exist:
            if source_directory.exists():
                if not source_directory.is_dir():
                    raise JournalUpgradeError(
                        "a legacy artifact remnant is not a directory"
                    )
                for index, path in enumerate(
                    sorted(source_directory.iterdir(), key=lambda item: item.name),
                    start=1,
                ):
                    if path.is_symlink() or not path.is_file():
                        raise JournalUpgradeError(
                            "a legacy artifact remnant is not a regular file"
                        )
                    try:
                        manifest.append(
                            describe_artifact(
                                artifact_id=f"legacy_orphan_{index:04d}",
                                path=path,
                                media_type="application/octet-stream",
                            )
                        )
                    except Exception as exc:
                        raise JournalUpgradeError(
                            "a legacy artifact remnant cannot be represented safely"
                        ) from exc
            artifact_state = "deleting"
            delete_requested_at = migrated.updated_at
        else:
            artifact_state = "deleted"
            deleted_at = migrated.updated_at
    return JobJournalV2(
        snapshot=migrated,
        enqueue_sequence=sequence,
        enqueue_sequence_source=source,
        artifact_state=artifact_state,
        artifact_manifest=manifest,
        artifact_delete_requested_at=delete_requested_at,
        artifacts_deleted_at=deleted_at,
    )


def plan_upgrades(
    job_root: Path,
    *,
    sequence_map: Mapping[str, Any] | None = None,
    recover_active: bool = False,
) -> tuple[list[PlannedUpgrade], dict[str, Any]]:
    mapping = _validate_sequence_map(sequence_map)
    discovered = discover_journal_paths(job_root)
    parsed: list[tuple[Path, bytes, JobJournalV2 | LegacyJobSnapshotV1]] = []
    used_sequences: set[int] = set()
    seen_job_ids: set[str] = set()
    v2_count = 0
    for path in discovered:
        content, raw = _read_journal(path)
        try:
            if raw.get("journal_schema_version") == 2:
                value: JobJournalV2 | LegacyJobSnapshotV1 = (
                    JobJournalV2.model_validate_json(content)
                )
                if value.enqueue_sequence in used_sequences:
                    raise JournalUpgradeError(
                        "duplicate enqueue sequence across durable journals"
                    )
                used_sequences.add(value.enqueue_sequence)
                v2_count += 1
            else:
                value = LegacyJobSnapshotV1.model_validate_json(content)
        except JournalUpgradeError:
            raise
        except Exception as exc:
            raise JournalUpgradeError("a durable journal violates its schema") from exc
        job_id = (
            value.snapshot.job_id if isinstance(value, JobJournalV2) else value.job_id
        )
        attempt_token = (
            value.snapshot.attempt_token
            if isinstance(value, JobJournalV2)
            else value.attempt_token
        )
        if job_id in seen_job_ids:
            raise JournalUpgradeError("duplicate job_id across durable journals")
        seen_job_ids.add(job_id)
        if path.parent.parent.name != job_id:
            raise JournalUpgradeError("journal directory and payload job_id differ")
        if path.parent.name != attempt_token:
            raise JournalUpgradeError(
                "journal directory and payload attempt_token differ"
            )
        parsed.append((path, content, value))

    legacy_job_ids = {
        value.job_id for _, _, value in parsed if isinstance(value, LegacyJobSnapshotV1)
    }
    unknown_mapping = set(mapping) - legacy_job_ids
    if unknown_mapping:
        raise JournalUpgradeError("the sequence map refers to an unknown durable job")
    active_legacy_jobs = [
        value.job_id
        for _, _, value in parsed
        if isinstance(value, LegacyJobSnapshotV1)
        and value.status in {"pending", "queued", "running", "cancel_requested"}
    ]
    if active_legacy_jobs and not recover_active:
        raise JournalUpgradeError(
            "active V1 journals were found; stop and inspect them or rerun with "
            "the explicit --recover-active mode"
        )

    upgrades: list[PlannedUpgrade] = []
    for path, content, value in parsed:
        if isinstance(value, JobJournalV2):
            continue
        journal = _migrate_legacy_snapshot(
            value,
            journal_path=path,
            mapped_sequence=mapping.get(value.job_id),
            used_sequences=used_sequences,
            recover_active=recover_active,
        )
        upgrades.append(
            PlannedUpgrade(path=path, original_bytes=content, journal=journal)
        )
    report = {
        "journal_count": len(discovered),
        "v1_count": len(upgrades),
        "v2_count": v2_count,
        "changes_required": bool(upgrades),
        "jobs": [
            {
                "job_id": item.journal.snapshot.job_id,
                "status": item.journal.snapshot.status,
                "enqueue_sequence": item.journal.enqueue_sequence,
                "enqueue_sequence_source": item.journal.enqueue_sequence_source,
                "artifact_state": item.journal.artifact_state,
            }
            for item in upgrades
        ],
    }
    return upgrades, report


def apply_upgrades(
    job_root: Path,
    upgrades: Sequence[PlannedUpgrade],
    *,
    backup_directory: Path,
) -> dict[str, Any]:
    root, dev_runtime_root = _validated_job_root(job_root)
    try:
        backup_root = validate_dev_runtime_path(
            "journal backup directory",
            Path(backup_directory),
            runtime_root=dev_runtime_root,
            leaf_kind="directory",
        )
    except ValueError as exc:
        raise JournalUpgradeError(str(exc)) from exc
    if backup_root.resolve().is_relative_to(root.resolve()):
        raise JournalUpgradeError("backup directory must be outside the job root")
    if backup_root.is_symlink() or backup_root.exists():
        raise JournalUpgradeError(
            "backup directory already exists or is a symbolic link"
        )
    if backup_root.parent.is_symlink():
        raise JournalUpgradeError("backup directory parent must not be a symbolic link")
    ensure_private_directory(backup_root.parent)
    backup_root.mkdir(mode=0o700)
    os.chmod(backup_root, 0o700)

    manifest_entries: list[dict[str, Any]] = []
    for item in upgrades:
        try:
            relative = item.path.relative_to(root)
        except ValueError as exc:
            raise JournalUpgradeError(
                "a journal is outside the selected job root"
            ) from exc
        destination = backup_root / relative
        if destination.is_symlink() or destination.exists():
            raise JournalUpgradeError(
                "a backup destination already exists or is unsafe"
            )
        atomic_write_bytes(destination, item.original_bytes)
        os.chmod(destination, 0o600)
        manifest_entries.append(
            {
                "path": relative.as_posix(),
                "size_bytes": len(item.original_bytes),
                "sha256": hashlib.sha256(item.original_bytes).hexdigest(),
            }
        )
    manifest_path = backup_root / "sha256-manifest.json"
    atomic_write_json(
        manifest_path,
        {
            "schema_version": 1,
            "source_job_root": str(root.resolve()),
            "journals": manifest_entries,
        },
    )
    os.chmod(manifest_path, 0o600)

    # Re-read the complete batch after backup and before the first replacement.
    # This protects direct API callers that planned outside the exclusive lock.
    for item in upgrades:
        current_bytes, _ = _read_journal(item.path)
        if current_bytes != item.original_bytes:
            raise JournalUpgradeError(
                "a journal changed after planning; no source journals were upgraded"
            )

    for item in upgrades:
        if item.path.is_symlink():
            raise JournalUpgradeError("a journal became a symbolic link during upgrade")
        atomic_write_json(item.path, item.journal.model_dump(mode="json"))
        os.chmod(item.path, 0o600)
    return {
        "upgraded": len(upgrades),
        "backup_directory": str(backup_root),
        "backup_manifest": str(manifest_path),
    }


def _load_sequence_map(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if path.is_symlink() or not path.is_file():
        raise JournalUpgradeError("sequence map must be a real JSON file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalUpgradeError("sequence map is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise JournalUpgradeError("sequence map must be a JSON object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or upgrade monomer DFT Worker journals without loading GPU models."
    )
    parser.add_argument("--job-root", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--sequence-map", type=Path)
    parser.add_argument(
        "--recover-active",
        action="store_true",
        help="fail interrupted jobs and resume mapped queued jobs during V1 migration",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.apply and arguments.backup_dir is None:
        print("--apply requires --backup-dir", file=sys.stderr)
        return 2
    try:
        _, dev_runtime_root = _validated_job_root(arguments.job_root)
        if arguments.sequence_map is not None:
            try:
                validate_dev_runtime_path(
                    "journal sequence map",
                    arguments.sequence_map,
                    runtime_root=dev_runtime_root,
                    leaf_kind="file",
                )
            except ValueError as exc:
                raise JournalUpgradeError(str(exc)) from exc
        sequence_map = _load_sequence_map(arguments.sequence_map)
        with JobRootLock(
            arguments.job_root,
            dev_runtime_root=dev_runtime_root,
        ):
            upgrades, report = plan_upgrades(
                arguments.job_root,
                sequence_map=sequence_map,
                recover_active=arguments.recover_active,
            )
            if arguments.apply:
                report["apply"] = apply_upgrades(
                    arguments.job_root,
                    upgrades,
                    backup_directory=arguments.backup_dir,
                )
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0
    except JournalUpgradeError as exc:
        print(json.dumps({"error": str(exc)}, separators=(",", ":")), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised through main().
    raise SystemExit(main())

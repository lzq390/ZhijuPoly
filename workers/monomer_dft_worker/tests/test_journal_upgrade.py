from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from workers.monomer_dft_worker.app.artifacts import (
    atomic_write_bytes,
    atomic_write_json,
)
from workers.monomer_dft_worker.app.journal_upgrade import (
    JobRootLock,
    JournalUpgradeError,
    apply_upgrades,
    main,
    plan_upgrades,
)
from workers.monomer_dft_worker.app.schemas import (
    LEGACY_TIMING_KEYS,
    TIMING_KEYS,
    JobJournalV2,
    LegacyJobSnapshotV1,
    LegacyJobSubmitRequestV1,
)


def _legacy_journal(
    root: Path,
    job_id: str,
    *,
    status: str = "completed",
) -> Path:
    token = hashlib.sha256(job_id.encode()).hexdigest()[:32]
    request = LegacyJobSubmitRequestV1(
        job_id=job_id,
        attempt_token=token,
        input={"smiles": "O", "net_charge": 0, "multiplicity": 1},
        calculation_type="single_point",
        single_point={"properties": ["energy"]},
    )
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    snapshot = LegacyJobSnapshotV1(
        job_id=job_id,
        attempt_token=token,
        request_sha256=request.request_sha256,
        worker_instance_id="legacy-worker",
        status=status,
        stage="artifacts" if status == "completed" else "queued",
        progress_percent=100 if status == "completed" else 0,
        created_at=now,
        updated_at=now,
        finished_at=now if status == "completed" else None,
        request=request,
        result={"schema_version": 1} if status == "completed" else None,
    )
    path = root / job_id / token / "journal.json"
    atomic_write_json(path, snapshot.model_dump(mode="json"))
    return path


def test_check_is_read_only_and_active_v1_requires_explicit_mode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "runs"
    completed = _legacy_journal(root, "completed")
    original = completed.read_bytes()

    assert main(["--job-root", str(root), "--check"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["v1_count"] == 1
    assert report["jobs"][0]["artifact_state"] == "deleted"
    assert completed.read_bytes() == original

    queued = _legacy_journal(root, "queued", status="queued")
    queued_original = queued.read_bytes()
    assert main(["--job-root", str(root), "--check"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert "active V1 journals" in error["error"]
    assert completed.read_bytes() == original
    assert queued.read_bytes() == queued_original


def test_apply_backs_up_before_v2_migration_and_preserves_scientific_v1(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    journal_path = _legacy_journal(root, "terminal-job")
    original = journal_path.read_bytes()
    backup = tmp_path / "journal-backups" / "20260102T030405Z"

    with JobRootLock(root):
        upgrades, report = plan_upgrades(root)
        assert report["v1_count"] == 1
        applied = apply_upgrades(root, upgrades, backup_directory=backup)

    assert applied["upgraded"] == 1
    assert os.stat(backup).st_mode & 0o777 == 0o700
    backup_journal = backup / journal_path.relative_to(root)
    assert backup_journal.read_bytes() == original
    assert os.stat(backup_journal).st_mode & 0o777 == 0o600
    manifest_path = backup / "sha256-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["journals"] == [
        {
            "path": journal_path.relative_to(root).as_posix(),
            "size_bytes": len(original),
            "sha256": hashlib.sha256(original).hexdigest(),
        }
    ]
    assert os.stat(manifest_path).st_mode & 0o777 == 0o600

    envelope = JobJournalV2.model_validate_json(journal_path.read_bytes())
    assert envelope.snapshot.schema_version == 2
    assert envelope.snapshot.request.schema_version == 2
    assert envelope.snapshot.result == {"schema_version": 1}
    assert envelope.snapshot.queue_position is None
    assert envelope.artifact_state == "deleted"
    assert envelope.artifacts_deleted_at is not None


def test_real_eight_key_v1_timings_upgrade_to_ten_key_v2_contract(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    journal_path = _legacy_journal(root, "legacy-eight-timings")
    raw_v1 = json.loads(journal_path.read_text(encoding="utf-8"))
    raw_v1["timings"] = {
        key: float(index + 1) for index, key in enumerate(LEGACY_TIMING_KEYS)
    }
    atomic_write_json(journal_path, raw_v1)

    assert set(raw_v1["timings"]) == set(LEGACY_TIMING_KEYS)
    assert "gpu_wait_ms" not in raw_v1["timings"]
    assert "model_load_ms" not in raw_v1["timings"]

    upgrades, report = plan_upgrades(root)
    migrated = upgrades[0].journal.snapshot

    assert report["v1_count"] == 1
    assert set(migrated.timings) == set(TIMING_KEYS)
    assert migrated.timings["gpu_wait_ms"] == 0.0
    assert migrated.timings["model_load_ms"] == 0.0
    for key in LEGACY_TIMING_KEYS:
        assert migrated.timings[key] == raw_v1["timings"][key]


def test_original_eight_key_journal_v2_remains_readable_without_rewrite(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    journal_path = _legacy_journal(root, "old-v2-timings")
    upgrades, _ = plan_upgrades(root)
    envelope = upgrades[0].journal
    raw = envelope.model_dump(mode="json")
    old_timings = {
        key: float(index + 1) for index, key in enumerate(LEGACY_TIMING_KEYS)
    }
    raw["snapshot"]["timings"] = old_timings
    raw["snapshot"]["result"] = {
        "schema_version": 2,
        "timings": old_timings,
    }
    atomic_write_json(journal_path, raw)
    original = journal_path.read_bytes()

    planned, report = plan_upgrades(root)

    assert planned == []
    assert report["v1_count"] == 0
    assert report["v2_count"] == 1
    assert report["changes_required"] is False
    assert journal_path.read_bytes() == original
    loaded = JobJournalV2.model_validate_json(original)
    assert set(loaded.snapshot.timings) == set(TIMING_KEYS)
    assert loaded.snapshot.timings["gpu_wait_ms"] == 0.0
    assert loaded.snapshot.timings["model_load_ms"] == 0.0
    assert set(loaded.snapshot.result["timings"]) == set(TIMING_KEYS)


def test_legacy_empty_manifest_with_disk_remnants_migrates_to_deleting(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    journal_path = _legacy_journal(root, "orphaned-files")
    artifact_directory = journal_path.parent / "artifacts"
    atomic_write_bytes(artifact_directory / "Result.JSON", b"legacy-result")
    atomic_write_bytes(journal_path.parent / "artifact_bundle.zip", b"legacy-bundle")

    upgrades, _ = plan_upgrades(root)
    envelope = upgrades[0].journal
    assert envelope.artifact_state == "deleting"
    assert envelope.snapshot.artifacts == []
    assert [item.name for item in envelope.artifact_manifest] == ["Result.JSON"]
    assert envelope.artifact_delete_requested_at is not None


def test_explicit_active_recovery_requires_authoritative_mapping_to_resume(
    tmp_path: Path,
) -> None:
    failed_root = tmp_path / "failed-runs"
    _legacy_journal(failed_root, "queued-without-sequence", status="queued")
    upgrades, _ = plan_upgrades(failed_root, recover_active=True)
    assert upgrades[0].journal.snapshot.status == "failed"
    assert (
        upgrades[0].journal.snapshot.error.code
        == "journal_upgrade_missing_enqueue_sequence"
    )

    mapped_root = tmp_path / "mapped-runs"
    _legacy_journal(mapped_root, "queued-with-sequence", status="queued")
    upgrades, _ = plan_upgrades(
        mapped_root,
        sequence_map={"queued-with-sequence": 17},
        recover_active=True,
    )
    assert upgrades[0].journal.snapshot.status == "queued"
    assert upgrades[0].journal.enqueue_sequence == 17
    assert upgrades[0].journal.enqueue_sequence_source == "legacy_mapping"


def test_offline_upgrade_lock_refuses_a_concurrent_worker(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _legacy_journal(root, "terminal")
    with JobRootLock(root):
        with pytest.raises(JournalUpgradeError, match="must be stopped"):
            with JobRootLock(root):
                pass


def test_offline_upgrade_rejects_production_job_root_before_access() -> None:
    with pytest.raises(JournalUpgradeError, match="production repository"):
        JobRootLock(
            Path("/data/lzq/gith/nexpoly/ops/state/monomer-dft-worker-runs")
        )


def test_apply_refuses_a_journal_changed_after_planning(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    journal = _legacy_journal(root, "changed-after-plan")
    upgrades, _ = plan_upgrades(root)
    changed = json.loads(journal.read_text(encoding="utf-8"))
    changed["updated_at"] = "2026-01-02T03:04:06Z"
    atomic_write_json(journal, changed)
    changed_bytes = journal.read_bytes()

    with pytest.raises(JournalUpgradeError, match="changed after planning"):
        apply_upgrades(
            root,
            upgrades,
            backup_directory=tmp_path / "backups" / "changed",
        )
    assert journal.read_bytes() == changed_bytes

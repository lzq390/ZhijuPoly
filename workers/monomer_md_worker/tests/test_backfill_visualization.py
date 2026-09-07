from __future__ import annotations

from copy import deepcopy
from multiprocessing import get_context
from pathlib import Path

import pytest

from workers.monomer_md_worker.app.backfill_visualization import backfill_candidate
from workers.monomer_md_worker.app.storage_lock import job_storage_lock


JOB_ID = "a" * 32


class FakeRepository:
    def __init__(self, snapshot: dict, *, conflict: bool = False) -> None:
        self.snapshot = deepcopy(snapshot)
        self.conflict = conflict
        self.updates: list[dict] = []

    def get_job(self, job_id: str):
        assert job_id == JOB_ID
        return deepcopy(self.snapshot)

    def update_visualization_cas(self, snapshot: dict, merged_result: dict) -> bool:
        self.updates.append(deepcopy(merged_result))
        if self.conflict:
            return False
        self.snapshot["result_data"] = deepcopy(merged_result)
        return True


def _snapshot(root: Path) -> dict:
    return {
        "job_id": JOB_ID,
        "protocol": "Density",
        "artifact_root": str(root),
        "status": "completed",
        "run_mode": "formal",
        "result_data": {"metrics": {"density": 1.02}, "summary": {"density": 1.02}},
        "artifact_deleted_at": None,
        "finished_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
    }


def _write_state(root: Path) -> None:
    outputs = root / "outputs"
    outputs.mkdir(parents=True)
    (outputs / "npt_state.csv").write_text(
        '#"Step","Time (ps)","Potential Energy (kJ/mole)","Kinetic Energy (kJ/mole)",'
        '"Total Energy (kJ/mole)","Temperature (K)","Density (g/mL)"\n'
        "10,0.02,-140,40,-100,298,1.0\n",
        encoding="utf-8",
    )


def _hold_storage_lock(root: Path, ready, release) -> None:
    with job_storage_lock(root, JOB_ID):
        ready.set()
        release.wait(10)


def _acquire_storage_lock(root: Path, acquired) -> None:
    with job_storage_lock(root, JOB_ID):
        acquired.set()


def test_backfill_dry_run_is_zero_write_and_reports_partial(tmp_path: Path) -> None:
    root = tmp_path / JOB_ID
    root.mkdir()
    _write_state(root)
    snapshot = _snapshot(root)
    repository = FakeRepository(snapshot)

    outcome = backfill_candidate(
        snapshot,
        repository=repository,  # type: ignore[arg-type]
        allowed_roots=[tmp_path],
        apply=False,
    )

    assert outcome.outcome == "partial"
    assert outcome.would_update is True
    assert outcome.payload_bytes > 0
    assert outcome.warnings == (
        "npt:TRAJECTORY_TOPOLOGY_MISSING",
        "npt:TRAJECTORY_DCD_MISSING",
    )
    assert repository.updates == []


def test_backfill_apply_merges_only_visualization_and_second_run_is_noop(
    tmp_path: Path,
) -> None:
    root = tmp_path / JOB_ID
    root.mkdir()
    _write_state(root)
    snapshot = _snapshot(root)
    repository = FakeRepository(snapshot)

    first = backfill_candidate(
        snapshot,
        repository=repository,  # type: ignore[arg-type]
        allowed_roots=[tmp_path],
        apply=True,
    )
    second = backfill_candidate(
        snapshot,
        repository=repository,  # type: ignore[arg-type]
        allowed_roots=[tmp_path],
        apply=True,
    )

    assert first.outcome == "updated"
    assert second.outcome == "already_v3"
    assert len(repository.updates) == 1
    merged = repository.updates[0]
    assert merged["metrics"] == {"density": 1.02}
    assert merged["summary"] == {"density": 1.02}
    assert merged["visualization"]["schema_version"] == 3


def test_backfill_reports_cas_conflict_without_retry(tmp_path: Path) -> None:
    root = tmp_path / JOB_ID
    root.mkdir()
    _write_state(root)
    snapshot = _snapshot(root)
    repository = FakeRepository(snapshot, conflict=True)

    outcome = backfill_candidate(
        snapshot,
        repository=repository,  # type: ignore[arg-type]
        allowed_roots=[tmp_path],
        apply=True,
    )

    assert outcome.outcome == "cas_conflict"
    assert len(repository.updates) == 1


def test_backfill_leaves_fully_unavailable_job_unchanged(tmp_path: Path) -> None:
    root = tmp_path / JOB_ID
    root.mkdir()
    snapshot = _snapshot(root)
    repository = FakeRepository(snapshot)

    outcome = backfill_candidate(
        snapshot,
        repository=repository,  # type: ignore[arg-type]
        allowed_roots=[tmp_path],
        apply=True,
    )

    assert outcome.outcome == "unavailable"
    assert outcome.would_update is False
    assert repository.updates == []


def test_backfill_rejects_deleted_and_unsafe_roots(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    unsafe = tmp_path / JOB_ID
    unsafe.symlink_to(real, target_is_directory=True)
    snapshot = _snapshot(unsafe)
    repository = FakeRepository(snapshot)

    unsafe_outcome = backfill_candidate(
        snapshot,
        repository=repository,  # type: ignore[arg-type]
        allowed_roots=[tmp_path],
        apply=False,
    )
    deleted = {**snapshot, "artifact_deleted_at": "2026-09-02T00:00:00Z"}
    deleted_outcome = backfill_candidate(
        deleted,
        repository=repository,  # type: ignore[arg-type]
        allowed_roots=[tmp_path],
        apply=False,
    )

    assert unsafe_outcome.outcome == "unsafe_root"
    assert deleted_outcome.outcome == "skip_deleted"


def test_job_storage_lock_serializes_backfill_and_artifact_deletion(
    tmp_path: Path,
) -> None:
    context = get_context("fork")
    ready = context.Event()
    release = context.Event()
    acquired = context.Event()
    holder = context.Process(target=_hold_storage_lock, args=(tmp_path, ready, release))
    contender = context.Process(target=_acquire_storage_lock, args=(tmp_path, acquired))
    holder.start()
    try:
        assert ready.wait(3)
        contender.start()
        assert not acquired.wait(0.2)
        release.set()
        assert acquired.wait(3)
    finally:
        release.set()
        holder.join(3)
        contender.join(3)
        if holder.is_alive():
            holder.terminate()
            holder.join()
        if contender.is_alive():
            contender.terminate()
            contender.join()
    assert holder.exitcode == 0
    assert contender.exitcode == 0


def test_job_storage_lock_rejects_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="job id is unsafe"):
        with job_storage_lock(tmp_path, "../escape"):
            pass

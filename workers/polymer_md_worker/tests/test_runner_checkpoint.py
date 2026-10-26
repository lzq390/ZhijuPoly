"""Prevent consumption of mutable or unaccepted scientific restart files."""
import hashlib
import json
from pathlib import Path

import pytest

from workers.polymer_md_worker.app.scientific_runner import load_state
from workers.polymer_md_worker.app import runtime


def test_restart_requires_exact_journal_checkpoint(tmp_path):
    request = {"smiles": "*CC*", "seed": 1729}
    initial = load_state("terminal_qm", None, None, request)
    initial["completed"] = ["terminal_qm"]
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps(initial))
    digest = runtime.sha256(path)
    assert load_state("monomer_qm", path, digest, request) == initial
    with pytest.raises(RuntimeError, match="immutable checkpoint"):
        load_state("monomer_qm", None, None, request)
    with pytest.raises(RuntimeError, match="stage identity"):
        load_state("build", path, digest, request)
    with pytest.raises(RuntimeError, match="different scientific request"):
        load_state("monomer_qm", path, digest, {**request, "seed": 1730})
    path.write_text(json.dumps({**initial, "molecules": {"cell": "tampered"}}))
    with pytest.raises(RuntimeError, match="unsafe checkpoint"):
        load_state("monomer_qm", path, digest, request)


def test_tc_cannot_guess_newest_analysis_from_files(tmp_path):
    request = {"smiles": "*CC*", "seed": 1729}
    state = load_state("terminal_qm", None, None, request)
    state["completed"] = ["terminal_qm", "analyze_0"]
    frozen = tmp_path / "accepted.json"
    frozen.write_text(json.dumps(state))
    # An interrupted newer analysis exists but was never journal-accepted.
    (tmp_path / "analyze_2").mkdir()
    (tmp_path / "analyze_2/checkpoint.json").write_text('{"unaccepted":true}')
    assert load_state("tc", frozen, runtime.sha256(frozen), request)["completed"][-1] == "analyze_0"


def test_runtime_cache_revalidates_changed_asset(tmp_path, monkeypatch):
    asset = tmp_path / "forcefield.json"
    asset.write_text('{"mass":12}')
    initial_stat = asset.stat()
    original_stat = Path.stat

    def unchanged_asset_stat(path, *args, **kwargs):
        # Reproduce coarse filesystem timestamps deterministically. Only this
        # asset is frozen; the manifest and unrelated files retain real stat.
        if path == asset:
            return initial_stat
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", unchanged_asset_stat)
    monkeypatch.setattr(runtime, "adapter_fingerprint", lambda: "test-adapter")
    manifest = {"source_sha": runtime.SOURCE_SHA, "patch_version": runtime.PATCH_VERSION,
        "adapter_fingerprint": "test-adapter", "engine_probe": {"passed": True},
        "verified_files": [{"path": str(asset), "sha256": runtime.sha256(asset)}]}
    manifest["runtime_fingerprint"] = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    assert runtime.probe_runtime(path)["ready"]
    asset.write_text('{"mass":99}')
    assert runtime.sha256(asset) != manifest["verified_files"][0]["sha256"]
    assert (asset.stat().st_mtime_ns, asset.stat().st_ctime_ns, asset.stat().st_size) == (
        initial_stat.st_mtime_ns, initial_stat.st_ctime_ns, initial_stat.st_size)
    result = runtime.probe_runtime(path)
    assert result["ready"] is False
    assert "runtime_file_integrity:forcefield.json" in result["errors"]

from __future__ import annotations

from dataclasses import replace
import itertools
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import mdtraj as md
import numpy as np

from workers.polymer_md_analysis.child import execute
from workers.polymer_md_analysis.config import GIB
from workers.polymer_md_analysis.contracts import AnalysisError, TrajectoryAnalysisRequest
from workers.polymer_md_analysis.files import METADATA_LIMIT, check_space, prepare_sources
from workers.polymer_md_analysis.files import strict_json
from workers.polymer_md_worker.app.scientific_analysis import analyze_distance
from workers.polymer_md_analysis.tests.helpers import JOB, TOKEN, fixture, query, settings


class SourcesAndNumerics(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = settings(self.root)
        self.body, self.directory, self.metadata = fixture(self.settings.job_root)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def test_chunked_distance_matches_cartesian_and_minimum_image_without_topology_copies(self):
        trajectory = md.load_xtc(str(self.directory / "trajectory.xtc"), top=str(self.directory / "cell.pdb"))
        for periodic in [False, True]:
            source = query(self.body, use_pbc=periodic)
            private = self.private / str(periodic)
            private.mkdir()
            result = prepare_sources(self.settings, source, private)
            with patch.object(md.Topology, "__deepcopy__", side_effect=AssertionError("per-frame topology copy")):
                data = analyze_distance(private, result, source.distance.model_dump())
            frames = np.arange(3, 204, 7)
            delta = np.abs(trajectory.xyz[frames, 0] - trajectory.xyz[frames, 1])
            expected = np.linalg.norm(np.minimum(delta, 1 - delta) if periodic else delta, axis=1) * 10
            np.testing.assert_allclose([p["value"] for p in data["points"]], expected, atol=1e-5)
            self.assertEqual([p["frame"] for p in data["points"]], frames.tolist())
            times = np.where(frames <= 100, frames * .001, .1 + (frames - 100) * .01)
            np.testing.assert_allclose([p["time_ps"] for p in data["points"]], times)
            self.assertEqual(set(p.name for p in private.iterdir()), {"trajectory.xtc", "cell.pdb"})

    def test_triclinic_minimum_image_matches_independent_neighbor_cell_enumeration(self):
        other = self.root / "triclinic"
        body, directory, _ = fixture(other, frames=4, triclinic=True)
        cfg = replace(self.settings, job_root=other)
        source = query(body, start_frame=0, stop_frame=4, stride=1)
        prepared = prepare_sources(cfg, source, self.private)
        data = analyze_distance(self.private, prepared, source.distance.model_dump())
        tr = md.load_xtc(str(directory / "trajectory.xtc"), top=str(directory / "cell.pdb"))
        images = np.array(list(itertools.product([-1, 0, 1], repeat=3)))
        expected = [min(np.linalg.norm(tr.xyz[i, 0] - tr.xyz[i, 1] + shift @ tr.unitcell_vectors[i]) for shift in images) * 10 for i in range(4)]
        np.testing.assert_allclose([p["value"] for p in data["points"]], expected, atol=1e-5)

    def test_source_unlink_after_all_descriptors_open_keeps_verified_private_inputs(self):
        def unlink(*args):
            (self.directory / "trajectory.xtc").unlink()
            (self.directory / "cell.pdb").unlink()
        with patch("workers.polymer_md_analysis.files.check_space", side_effect=unlink):
            result = prepare_sources(self.settings, query(self.body), self.private)
        data = analyze_distance(self.private, result, query(self.body).distance.model_dump())
        self.assertEqual(len(data["points"]), 29)

    def test_manifest_identity_and_sha_changes_fail_without_using_another_stage(self):
        self.body["artifacts"][0]["sha256"] = "0" * 64
        with self.assertRaises(AnalysisError) as caught:
            prepare_sources(self.settings, query(self.body), self.private)
        self.assertEqual(caught.exception.code, "artifact_integrity_mismatch")

    def test_missing_or_duplicate_exact_source_is_rejected(self):
        for artifacts in [self.body["artifacts"][1:], self.body["artifacts"] + [{**self.body["artifacts"][0], "artifact_id": "duplicate"}]]:
            with self.subTest(artifacts=len(artifacts)):
                with self.assertRaises(AnalysisError) as caught:
                    prepare_sources(self.settings, query({**self.body, "artifacts": artifacts}), self.private)
                self.assertEqual(caught.exception.code, "artifact_identity_mismatch")

    def test_source_ancestor_symlink_is_rejected(self):
        target = self.directory.with_name("original")
        self.directory.rename(target)
        self.directory.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            prepare_sources(self.settings, query(self.body), self.private)

    def test_metadata_identity_size_and_range_are_validated_before_copy(self):
        path = self.settings.job_root / str(JOB) / TOKEN / "stages/eq1-1/result.json"
        path.write_text(json.dumps({"stage": "eq2", "results": {"trajectory": self.metadata}}))
        with self.assertRaises(AnalysisError) as caught:
            prepare_sources(self.settings, query(self.body), self.private)
        self.assertEqual(caught.exception.code, "artifact_identity_mismatch")
        with path.open("wb") as handle:
            handle.truncate(METADATA_LIMIT + 1)
        with self.assertRaises(AnalysisError) as caught:
            prepare_sources(self.settings, query(self.body), self.private)
        self.assertEqual(caught.exception.status, 422)
        path.write_text(json.dumps({"stage": "eq1", "results": {"trajectory": self.metadata}}))
        with self.assertRaises(AnalysisError):
            prepare_sources(self.settings, query(self.body, stop_frame=300), self.private)
        self.assertFalse(list(self.private.iterdir()))

    def test_metadata_mutation_during_parse_is_rejected(self):
        path = self.settings.job_root / str(JOB) / TOKEN / "stages/eq1-1/result.json"
        def mutate(raw):
            value = strict_json(raw)
            path.write_bytes(b"changed during parsing")
            return value
        with patch("workers.polymer_md_analysis.files.strict_json", side_effect=mutate):
            with self.assertRaises(AnalysisError) as caught:
                prepare_sources(self.settings, query(self.body), self.private)
        self.assertEqual(caught.exception.code, "artifact_identity_mismatch")
        self.assertFalse(list(self.private.iterdir()))

    def test_actual_frame_count_is_checked_even_for_partial_distance_range(self):
        path = self.settings.job_root / str(JOB) / TOKEN / "stages/eq1-1/result.json"
        path.write_text(json.dumps({"stage": "eq1", "results": {"trajectory": {**self.metadata, "frame_count": 206}}}))
        cfg = {"job_root": str(self.settings.job_root), "spool_root": str(self.settings.spool_root), "spool_budget_bytes": self.settings.spool_budget_bytes, "min_free_bytes": 0, "free_fraction": 0}
        with self.assertRaises(AnalysisError) as caught:
            execute({"operation": "distance", "settings": cfg, "request": query(self.body).model_dump(mode="json"), "private": str(self.private)})
        self.assertEqual(caught.exception.code, "invalid_analysis_data")

    def test_full_contract_size_has_no_new_small_input_ceiling_and_disk_reserve_is_enforced(self):
        usage = shutil._ntuple_diskusage(200 * GIB, 50 * GIB, 150 * GIB)
        with patch("workers.polymer_md_analysis.files.shutil.disk_usage", return_value=usage):
            check_space(self.private, 40 * GIB + 32 * 1024**2, 48 * GIB, GIB, .05)
            with self.assertRaises(AnalysisError) as caught:
                check_space(self.private, 49 * GIB, 48 * GIB, GIB, .05)
            self.assertEqual(caught.exception.code, "analysis_storage_busy")
        with patch("workers.polymer_md_analysis.files.shutil.disk_usage", return_value=shutil._ntuple_diskusage(200 * GIB, 195 * GIB, 5 * GIB)):
            with self.assertRaises(AnalysisError):
                check_space(self.private, GIB, 48 * GIB, GIB, .05)

    def test_trajectory_and_distance_child_outputs_retain_source_and_clock(self):
        cfg = {"job_root": str(self.settings.job_root), "spool_root": str(self.settings.spool_root), "spool_budget_bytes": self.settings.spool_budget_bytes, "min_free_bytes": 0, "free_fraction": 0}
        source = TrajectoryAnalysisRequest.model_validate({**self.body, "frame_index": 101, "limit": 1})
        frame = execute({"operation": "trajectory", "settings": cfg, "request": source.model_dump(mode="json"), "private": str(self.private)})
        self.assertEqual(frame["job_id"], str(JOB)); self.assertEqual(frame["stage_run_id"], "eq1-1")
        self.assertEqual(frame["frame_count"], 205); self.assertEqual(len(frame["points"]), 1)
        self.assertAlmostEqual(frame["time_ps"], .11)


if __name__ == "__main__":
    unittest.main()

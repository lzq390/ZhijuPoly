from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_PATH = REPO_ROOT / "scripts" / "monomer_md_smoke.py"
SPEC = importlib.util.spec_from_file_location("monomer_md_smoke", SMOKE_PATH)
assert SPEC and SPEC.loader
monomer_md_smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monomer_md_smoke)


class MonomerMdSmokeTests(unittest.TestCase):
    def test_authoritative_smoke_validates_steps_artifacts_and_capacity(self) -> None:
        responses = [
            {"available": True, "can_submit": True, "default_steps": 300},
            {"job_id": "job-1", "status": "submitted"},
            {
                "job_id": "job-1",
                "status": "completed",
                "requested_steps": 300,
                "completed_steps": 300,
                "byteff2_git_sha": "a" * 40,
                "result": {
                    "summary": {"n_steps": 300},
                    "not_equilibrated": True,
                    "physical_density_estimate": False,
                    "warnings": ["demo only"],
                },
                "artifacts": {
                    "state": {"path": "npt_state.csv"},
                    "trajectory": {"path": "npt.dcd"},
                },
            },
            {"active_jobs": 0, "database_active_jobs": 0, "can_submit": True},
        ]
        with mock.patch.object(monomer_md_smoke, "request_json", side_effect=responses):
            job_id = monomer_md_smoke.run_smoke("http://example", 30, "a" * 40)

        self.assertEqual(job_id, "job-1")


if __name__ == "__main__":
    unittest.main()

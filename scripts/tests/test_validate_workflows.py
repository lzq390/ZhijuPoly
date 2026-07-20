from __future__ import annotations

from pathlib import Path
import unittest

from scripts.ci import validate_workflows as policy


ROOT = Path(__file__).resolve().parents[2]
CI_TEXT = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
EXACT_B_TEXT = (ROOT / "scripts/ci/test_exact_b_bridge.sh").read_text(
    encoding="utf-8"
)


class StructuredWorkflowPolicyTests(unittest.TestCase):
    def test_complete_history_is_bound_to_the_three_consuming_jobs(self) -> None:
        failures: list[str] = []
        policy.validate_complete_history_checkouts(CI_TEXT, failures)
        self.assertEqual(failures, [])

    def test_global_fetch_depth_count_cannot_mask_the_wrong_job(self) -> None:
        script_job = CI_TEXT.index("  script-tests:\n")
        script_fetch = CI_TEXT.index("fetch-depth: 0", script_job)
        changed = (
            CI_TEXT[:script_fetch]
            + "fetch-depth: 1"
            + CI_TEXT[script_fetch + len("fetch-depth: 0") :]
        )
        policy_job = changed.index("  policy:\n")
        policy_credentials = changed.index(
            "persist-credentials: false",
            policy_job,
        )
        insertion = policy_credentials + len("persist-credentials: false")
        changed = changed[:insertion] + "\n          fetch-depth: 0" + changed[insertion:]
        self.assertEqual(
            changed.count("fetch-depth: 0"),
            CI_TEXT.count("fetch-depth: 0"),
        )

        failures: list[str] = []
        policy.validate_complete_history_checkouts(changed, failures)
        self.assertTrue(
            any(
                "script-tests must have one exact candidate checkout" in failure
                for failure in failures
            ),
            failures,
        )

    def test_exact_b_job_steps_are_structurally_ordered(self) -> None:
        failures: list[str] = []
        policy.validate_exact_b_job(CI_TEXT, failures)
        self.assertEqual(failures, [])

        login = "Log in for the exact private B images"
        transition = "Run real B-schema and F/0013-schema transition smoke"
        changed = CI_TEXT.replace(login, "TEMPORARY", 1)
        changed = changed.replace(transition, login, 1)
        changed = changed.replace("TEMPORARY", transition, 1)
        failures = []
        policy.validate_exact_b_job(changed, failures)
        self.assertTrue(
            any("missing or reorders" in failure for failure in failures),
            failures,
        )


class ExactBTransitionPolicyTests(unittest.TestCase):
    def test_current_transition_and_mutable_digest_are_complete(self) -> None:
        failures: list[str] = []
        policy.validate_exact_b_transition(EXACT_B_TEXT, failures)
        self.assertEqual(failures, [])

    def test_deleted_b_to_f_transition_cannot_pass_on_legacy_markers(self) -> None:
        start_marker = (
            'b_transition_before="$(pre_dft_mutable_digest "$B_DATABASE")"'
        )
        end_marker = (
            'run_backend_command "$F_BACKEND_IMAGE" "$F_DATABASE" \\\n'
            "  python -m app.postgres_migrations --mode bootstrap"
        )
        start = EXACT_B_TEXT.index(start_marker)
        end = EXACT_B_TEXT.index(end_marker, start)
        changed = EXACT_B_TEXT[:start] + EXACT_B_TEXT[end:]
        self.assertIn('business_digest "$F_DATABASE"', changed)
        self.assertIn("schema_not_ready", changed)

        failures: list[str] = []
        policy.validate_exact_b_transition(changed, failures)
        self.assertTrue(
            any("one exact start and end boundary" in failure for failure in failures),
            failures,
        )

    def test_mutable_digest_cannot_drop_a_governed_relation(self) -> None:
        start = EXACT_B_TEXT.index("pre_dft_mutable_digest() {")
        end = EXACT_B_TEXT.index("business_digest() {", start)
        section = EXACT_B_TEXT[start:end]
        changed_section = section.replace("'lab_sample_measurements'", "'removed'", 1)
        changed = EXACT_B_TEXT[:start] + changed_section + EXACT_B_TEXT[end:]

        failures: list[str] = []
        policy.validate_exact_b_transition(changed, failures)
        self.assertTrue(
            any("lab_sample_measurements" in failure for failure in failures),
            failures,
        )


if __name__ == "__main__":
    unittest.main()

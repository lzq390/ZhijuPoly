from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import unittest

from scripts.ci import validate_workflows as policy


ROOT = Path(__file__).resolve().parents[2]
CI_TEXT = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
DOCKERFILE_TEXT = (ROOT / "Dockerfile").read_text(encoding="utf-8")
BACKEND_IMAGE_ASSERTION = (
    ROOT / "scripts/ci/assert_backend_image_identity.sh"
).read_bytes()
BACKEND_IMAGE_ASSERTION_PATH = (
    ROOT / "scripts/ci/assert_backend_image_identity.sh"
)
POSTGRES_CLIENT_BOOTSTRAP = (
    ROOT / "scripts/ci/ensure_postgresql_16_client.sh"
).read_bytes()
EXACT_B_TEXT = (ROOT / "scripts/ci/test_exact_b_bridge.sh").read_text(
    encoding="utf-8"
)
DEPLOYMENT_TEXT = (ROOT / "docs/deployment.md").read_text(encoding="utf-8")
RELEASE_CONTROLLER_TEXT = (ROOT / "docs/release-controller.md").read_text(
    encoding="utf-8"
)
SOURCE_SUCCESSOR_TEXT = (
    ROOT / "scripts/adopt_git_permission_source_successor.py"
).read_text(encoding="utf-8")
ADOPT_RUNTIME_PREREQUISITES_TEXT = (
    ROOT / "scripts/adopt_runtime_prerequisites.py"
).read_text(encoding="utf-8")
PULL_DEPLOY_CONTROLLER_TEXT = (
    ROOT / "scripts/pull_deploy_controller.py"
).read_text(encoding="utf-8")


class StructuredWorkflowPolicyTests(unittest.TestCase):
    def test_adopted_git_permission_documentation_is_complete(self) -> None:
        failures: list[str] = []
        policy.validate_adopted_permission_documentation_text(
            DEPLOYMENT_TEXT,
            RELEASE_CONTROLLER_TEXT,
            failures,
        )
        self.assertEqual(failures, [])

    def test_adopted_git_permission_docs_require_both_confirmations(self) -> None:
        changed = DEPLOYMENT_TEXT.replace(
            "  --confirm-permission-impact-sha256 sha256:<reviewed-impact-digest>\n",
            "",
            1,
        )
        failures: list[str] = []
        policy.validate_adopted_permission_documentation_text(
            changed,
            RELEASE_CONTROLLER_TEXT,
            failures,
        )
        self.assertTrue(
            any("plan and impact confirmations" in failure for failure in failures),
            failures,
        )

    def test_source_successor_docs_require_both_confirmations(self) -> None:
        changed = DEPLOYMENT_TEXT.replace(
            "  --confirm-source-successor-impact-sha256 "
            "sha256:<reviewed-impact-digest>\n",
            "",
            1,
        )
        failures: list[str] = []
        policy.validate_adopted_permission_documentation_text(
            changed,
            RELEASE_CONTROLLER_TEXT,
            failures,
        )
        self.assertTrue(
            any(
                "source-successor apply and abort" in failure
                for failure in failures
            ),
            failures,
        )

    def test_source_successor_docs_bind_candidate_nonexecution(self) -> None:
        changed = DEPLOYMENT_TEXT.replace(
            "standard-library-only",
            "candidate-powered",
            1,
        )
        failures: list[str] = []
        policy.validate_adopted_permission_documentation_text(
            changed,
            RELEASE_CONTROLLER_TEXT,
            failures,
        )
        self.assertTrue(
            any("standard-library-only" in failure for failure in failures),
            failures,
        )

    def test_source_successor_docs_keep_old_and_current_trust_separate(
        self,
    ) -> None:
        changed = DEPLOYMENT_TEXT.replace(
            "separate evidence and must not be compared for equality",
            "interchangeable evidence that may be compared for equality",
            1,
        )
        failures: list[str] = []
        policy.validate_adopted_permission_documentation_text(
            changed,
            RELEASE_CONTROLLER_TEXT,
            failures,
        )
        self.assertTrue(
            any("must not be compared for equality" in failure for failure in failures),
            failures,
        )

    def test_source_successor_docs_pin_the_fixed_13_file_manifest(
        self,
    ) -> None:
        changed = RELEASE_CONTROLLER_TEXT.replace(
            "fixed 13-file manifest",
            "dynamically discovered manifest",
            1,
        )
        failures: list[str] = []
        policy.validate_adopted_permission_documentation_text(
            DEPLOYMENT_TEXT,
            changed,
            failures,
        )
        self.assertTrue(
            any("fixed 13-file manifest" in failure for failure in failures),
            failures,
        )

    def test_source_successor_docs_require_unit_schema_v2_and_v4_v3(
        self,
    ) -> None:
        mutations = (
            (
                "`state/adopted-unit-permissions.json` authority must all use "
                "schema v2",
                "`state/adopted-unit-permissions.json` authority may use "
                "schema v1",
            ),
            ("descriptor v4", "descriptor v3"),
            ("current-state v3", "current-state v2"),
        )
        for old, new in mutations:
            with self.subTest(contract=old):
                changed = DEPLOYMENT_TEXT.replace(old, new, 1)
                failures: list[str] = []
                policy.validate_adopted_permission_documentation_text(
                    changed,
                    RELEASE_CONTROLLER_TEXT,
                    failures,
                )
                self.assertTrue(failures)

    def test_source_successor_docs_pin_all_eight_lineage_anchors(
        self,
    ) -> None:
        for anchor in (
            "`source_successor_authority_sha256`",
            "`source_successor_completed_journal_sha256`",
            "`unit_permission_authority_sha256`",
            "`unit_permission_completed_journal_sha256`",
            "`unit_permission_transaction_inventory_sha256`",
            "`production_git_snapshot_authority_sha256`",
            "`bootstrap_router_intent_sha256`",
            "`bootstrap_router_authority_sha256`",
        ):
            with self.subTest(anchor=anchor):
                changed = RELEASE_CONTROLLER_TEXT.replace(
                    anchor,
                    "`removed_lineage_anchor`",
                    1,
                )
                failures: list[str] = []
                policy.validate_adopted_permission_documentation_text(
                    DEPLOYMENT_TEXT,
                    changed,
                    failures,
                )
                self.assertTrue(
                    any(anchor in failure for failure in failures),
                    failures,
                )

    def test_source_successor_docs_make_written_lineage_permanent(
        self,
    ) -> None:
        mutations = (
            (
                "Historical current-state v3 records may\nomit",
                "Historical current-state v3 records must\ninvent",
            ),
            (
                "once written, all eight\nanchors are permanent",
                "all eight anchors may later be\nremoved",
            ),
        )
        for old, new in mutations:
            with self.subTest(contract=old):
                changed = DEPLOYMENT_TEXT.replace(old, new, 1)
                failures: list[str] = []
                policy.validate_adopted_permission_documentation_text(
                    changed,
                    RELEASE_CONTROLLER_TEXT,
                    failures,
                )
                self.assertTrue(
                    any(old.split()[0] in failure for failure in failures),
                    failures,
                )

    def test_first_deployment_commands_are_strictly_ordered(self) -> None:
        rehearsal_plan = policy.ORDINARY_DEPLOYMENT_COMMANDS[2]
        rehearsal_apply = policy.ORDINARY_DEPLOYMENT_COMMANDS[3]
        for label, deployment, controller in (
            (
                "deployment",
                DEPLOYMENT_TEXT,
                RELEASE_CONTROLLER_TEXT,
            ),
            (
                "release-controller",
                DEPLOYMENT_TEXT,
                RELEASE_CONTROLLER_TEXT,
            ),
        ):
            with self.subTest(document=label):
                original = deployment if label == "deployment" else controller
                changed = original.replace(rehearsal_plan, "ORDER-TOKEN", 1)
                changed = changed.replace(rehearsal_apply, rehearsal_plan, 1)
                changed = changed.replace("ORDER-TOKEN", rehearsal_apply, 1)
                self.assertEqual(changed.count(rehearsal_plan), 1)
                self.assertEqual(changed.count(rehearsal_apply), 1)
                failures: list[str] = []
                policy.validate_adopted_permission_documentation_text(
                    changed if label == "deployment" else deployment,
                    changed if label == "release-controller" else controller,
                    failures,
                )
                self.assertTrue(
                    any(
                        "must order one exact direct plan" in failure
                        for failure in failures
                    ),
                    failures,
                )

    def test_successor_and_unit_authorities_precede_first_deployment(
        self,
    ) -> None:
        successor_apply = policy.SOURCE_SUCCESSOR_APPLY_COMMAND
        unit_plan = policy.UNIT_PERMISSION_PLAN_COMMAND
        for label, original in (
            ("deployment", DEPLOYMENT_TEXT),
            ("release-controller", RELEASE_CONTROLLER_TEXT),
        ):
            with self.subTest(document=label):
                changed = original.replace(successor_apply, "ORDER-TOKEN", 1)
                changed = changed.replace(unit_plan, successor_apply, 1)
                changed = changed.replace("ORDER-TOKEN", unit_plan, 1)
                self.assertEqual(changed.count(successor_apply), 1)
                self.assertEqual(changed.count(unit_plan), 1)
                failures: list[str] = []
                policy.validate_adopted_permission_documentation_text(
                    changed if label == "deployment" else DEPLOYMENT_TEXT,
                    (
                        changed
                        if label == "release-controller"
                        else RELEASE_CONTROLLER_TEXT
                    ),
                    failures,
                )
                self.assertTrue(
                    any(
                        "must order one exact snapshot"
                        in failure
                        for failure in failures
                    ),
                    failures,
                )

    def test_source_successor_docs_freeze_main_until_first_current_state(
        self,
    ) -> None:
        for label, original in (
            ("deployment", DEPLOYMENT_TEXT),
            ("release-controller", RELEASE_CONTROLLER_TEXT),
        ):
            for marker in policy.SOURCE_SUCCESSOR_MAIN_FREEZE_MARKERS:
                with self.subTest(document=label, marker=marker):
                    marker_pattern = r"\s+".join(
                        re.escape(token) for token in marker.split()
                    )
                    changed, replacements = re.subn(
                        marker_pattern,
                        "removed-source-successor-main-freeze-contract",
                        original,
                    )
                    self.assertGreaterEqual(replacements, 1)
                    failures: list[str] = []
                    policy.validate_adopted_permission_documentation_text(
                        changed if label == "deployment" else DEPLOYMENT_TEXT,
                        (
                            changed
                            if label == "release-controller"
                            else RELEASE_CONTROLLER_TEXT
                        ),
                        failures,
                    )
                    self.assertTrue(
                        any(marker in failure for failure in failures),
                        failures,
                    )

    def test_control_chain_only_docs_do_not_authorize_mutating_commands(
        self,
    ) -> None:
        for label, original in (
            ("deployment", DEPLOYMENT_TEXT),
            ("release-controller", RELEASE_CONTROLLER_TEXT),
        ):
            for marker in policy.CONTROL_CHAIN_ONLY_COMMAND_MARKERS:
                with self.subTest(document=label, marker=marker):
                    self.assertEqual(original.count(marker), 1)
                    changed = original.replace(
                        marker,
                        "removed-control-chain-only-command-warning",
                        1,
                    )
                    failures: list[str] = []
                    policy.validate_adopted_permission_documentation_text(
                        changed if label == "deployment" else DEPLOYMENT_TEXT,
                        (
                            changed
                            if label == "release-controller"
                            else RELEASE_CONTROLLER_TEXT
                        ),
                        failures,
                    )
                    self.assertTrue(
                        any(marker in failure for failure in failures),
                        failures,
                    )

    def test_mutable_role_precedes_first_deployment(self) -> None:
        for label, original, role_marker in (
            (
                "deployment",
                DEPLOYMENT_TEXT,
                policy.MUTABLE_ROLE_PLAN_COMMAND,
            ),
            (
                "release-controller",
                RELEASE_CONTROLLER_TEXT,
                policy.CONTROLLER_MUTABLE_ROLE_MARKER,
            ),
        ):
            with self.subTest(document=label):
                unit_apply = policy.UNIT_PERMISSION_APPLY_COMMAND
                changed = original.replace(unit_apply, "ORDER-TOKEN", 1)
                changed = changed.replace(role_marker, unit_apply, 1)
                changed = changed.replace("ORDER-TOKEN", role_marker, 1)
                failures: list[str] = []
                policy.validate_adopted_permission_documentation_text(
                    changed if label == "deployment" else DEPLOYMENT_TEXT,
                    (
                        changed
                        if label == "release-controller"
                        else RELEASE_CONTROLLER_TEXT
                    ),
                    failures,
                )
                self.assertTrue(
                    any("must order one exact snapshot" in failure for failure in failures),
                    failures,
                )

    def test_unit_permission_docs_require_exact_abort(self) -> None:
        changed = DEPLOYMENT_TEXT.replace(
            policy.UNIT_PERMISSION_ABORT_COMMAND,
            "removed-unit-permission-abort",
            1,
        )
        failures: list[str] = []
        policy.validate_adopted_permission_documentation_text(
            changed,
            RELEASE_CONTROLLER_TEXT,
            failures,
        )
        self.assertTrue(
            any(
                "deployment runbook also carrying exact abort" in failure
                for failure in failures
            ),
            failures,
        )

    def test_successor_implementation_contract_is_cross_checked(self) -> None:
        failures: list[str] = []
        policy.validate_successor_authority_contract_source_text(
            SOURCE_SUCCESSOR_TEXT,
            ADOPT_RUNTIME_PREREQUISITES_TEXT,
            PULL_DEPLOY_CONTROLLER_TEXT,
            failures,
        )
        self.assertEqual(failures, [])

    def test_successor_implementation_rejects_manifest_drift(self) -> None:
        mutations = (
            (
                SOURCE_SUCCESSOR_TEXT.replace(
                    '    "scripts/bridge_deploy_core.py",\n',
                    '    "scripts/unreviewed_dynamic_file.py",\n',
                    1,
                ),
                ADOPT_RUNTIME_PREREQUISITES_TEXT,
                PULL_DEPLOY_CONTROLLER_TEXT,
            ),
            (
                SOURCE_SUCCESSOR_TEXT,
                ADOPT_RUNTIME_PREREQUISITES_TEXT.replace(
                    '    "scripts/bridge_deploy_core.py",\n',
                    '    "scripts/unreviewed_dynamic_file.py",\n',
                    1,
                ),
                PULL_DEPLOY_CONTROLLER_TEXT,
            ),
            (
                SOURCE_SUCCESSOR_TEXT,
                ADOPT_RUNTIME_PREREQUISITES_TEXT,
                PULL_DEPLOY_CONTROLLER_TEXT.replace(
                    '+ ("scripts/bridge_deploy_core.py",)',
                    '+ ("scripts/unreviewed_dynamic_file.py",)',
                    1,
                ),
            ),
        )
        for successor, prerequisites, controller in mutations:
            with self.subTest(
                changed_source=(
                    "publisher"
                    if successor != SOURCE_SUCCESSOR_TEXT
                    else "adopter"
                    if prerequisites != ADOPT_RUNTIME_PREREQUISITES_TEXT
                    else "controller"
                )
            ):
                failures: list[str] = []
                policy.validate_successor_authority_contract_source_text(
                    successor,
                    prerequisites,
                    controller,
                    failures,
                )
                self.assertTrue(
                    any(
                        "fixed 13-file manifest" in failure
                        for failure in failures
                    ),
                    failures,
                )

    def test_successor_implementation_rejects_equal_trust_proofs(self) -> None:
        changed = SOURCE_SUCCESSOR_TEXT.replace(
            "sha256:ba12709eb87ebc3ca51ac6ebcaca425be50487420c3529b80ec8696cb8602a3b",
            "sha256:dd8c493199fd02daf621e7ffbcd51ca35ebf7da0e6f77fefd0759137c7a408d4",
            1,
        )
        failures: list[str] = []
        policy.validate_successor_authority_contract_source_text(
            changed,
            ADOPT_RUNTIME_PREREQUISITES_TEXT,
            PULL_DEPLOY_CONTROLLER_TEXT,
            failures,
        )
        self.assertTrue(
            any("two distinct digest proofs" in failure for failure in failures),
            failures,
        )

    def test_successor_implementation_rejects_unit_schema_drift(self) -> None:
        changed = ADOPT_RUNTIME_PREREQUISITES_TEXT.replace(
            "        schema_version = (\n"
            "            2\n"
            "            if \"adopted_git_permission_source_successor_sha256\" "
            "in context\n",
            "        schema_version = (\n"
            "            3\n"
            "            if \"adopted_git_permission_source_successor_sha256\" "
            "in context\n",
            1,
        )
        failures: list[str] = []
        policy.validate_successor_authority_contract_source_text(
            SOURCE_SUCCESSOR_TEXT,
            changed,
            PULL_DEPLOY_CONTROLLER_TEXT,
            failures,
        )
        self.assertTrue(
            any(
                "final authority must use schema v2" in failure
                for failure in failures
            ),
            failures,
        )

    def test_successor_implementation_rejects_descriptor_or_lineage_drift(
        self,
    ) -> None:
        mutations = (
            (
                "DESCRIPTOR_SCHEMA_VERSION = 4",
                "DESCRIPTOR_SCHEMA_VERSION = 3",
                "descriptor v4",
            ),
            (
                '\nADOPTION_SUCCESSOR_LINEAGE_FIELDS = {\n'
                '    "schema_version",\n'
                '    "source_successor_authority_sha256",\n'
                '    "source_successor_completed_journal_sha256",\n',
                '\nADOPTION_SUCCESSOR_LINEAGE_FIELDS = {\n'
                '    "schema_version",\n'
                '    "source_successor_authority_sha256",\n'
                '    "removed_completed_journal_anchor",\n',
                "eight permanent raw-authority anchors",
            ),
            (
                '    "unit_permission_transaction_inventory_sha256",\n'
                '    "production_git_snapshot_authority_sha256",\n',
                '    "removed_unit_transaction_inventory_anchor",\n'
                '    "production_git_snapshot_authority_sha256",\n',
                "eight permanent raw-authority anchors",
            ),
            (
                'if unit is None or unit.get("schema_version") != 2:',
                'if unit is None or unit.get("schema_version") != 1:',
                "reject a non-v2 unit authority",
            ),
        )
        for old, new, expected_failure in mutations:
            with self.subTest(contract=old):
                changed = PULL_DEPLOY_CONTROLLER_TEXT.replace(old, new, 1)
                self.assertNotEqual(changed, PULL_DEPLOY_CONTROLLER_TEXT)
                failures: list[str] = []
                policy.validate_successor_authority_contract_source_text(
                    SOURCE_SUCCESSOR_TEXT,
                    ADOPT_RUNTIME_PREREQUISITES_TEXT,
                    changed,
                    failures,
                )
                self.assertTrue(
                    any(expected_failure in failure for failure in failures),
                    failures,
                )

    def test_adopted_git_permission_docs_reject_legacy_execution(self) -> None:
        insertion = "./scripts/install_legacy_takeover_prerequisites.py \\\n"
        marker = "Next provision the dedicated mutable-data audit login."
        changed = DEPLOYMENT_TEXT.replace(marker, insertion + marker, 1)
        failures: list[str] = []
        policy.validate_adopted_permission_documentation_text(
            changed,
            RELEASE_CONTROLLER_TEXT,
            failures,
        )
        self.assertTrue(
            any("must not execute" in failure for failure in failures),
            failures,
        )

    def test_adopted_git_permission_docs_bind_exact_chmod_scope(self) -> None:
        changed = DEPLOYMENT_TEXT.replace(".git/**", ".git/*", 1)
        failures: list[str] = []
        policy.validate_adopted_permission_documentation_text(
            changed,
            RELEASE_CONTROLLER_TEXT,
            failures,
        )
        self.assertTrue(
            any("checkout root and `.git/**`" in failure for failure in failures),
            failures,
        )

    def test_adopted_git_permission_docs_forbid_old_controller_probe(
        self,
    ) -> None:
        changed = DEPLOYMENT_TEXT.replace(
            "does not invoke that old controller",
            "may invoke that old controller",
            1,
        )
        failures: list[str] = []
        policy.validate_adopted_permission_documentation_text(
            changed,
            RELEASE_CONTROLLER_TEXT,
            failures,
        )
        self.assertTrue(
            any("does not invoke" in failure for failure in failures),
            failures,
        )

    def test_complete_history_is_bound_to_all_consuming_jobs(self) -> None:
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

    def test_script_tests_keep_fault_injection_budget(self) -> None:
        script_job = CI_TEXT.index("  script-tests:\n")
        timeout = CI_TEXT.index("    timeout-minutes: 40\n", script_job)
        changed = (
            CI_TEXT[:timeout]
            + "    timeout-minutes: 20\n"
            + CI_TEXT[timeout + len("    timeout-minutes: 40\n") :]
        )
        failures: list[str] = []
        policy.validate_script_tests_budget(changed, failures)
        self.assertTrue(
            any("40-minute fault-injection" in failure for failure in failures),
            failures,
        )

    def test_script_tests_cannot_exclude_successor_contracts(self) -> None:
        marker = "              ! -name 'test_dev_gpu_session.py' \\\n"
        changed = CI_TEXT.replace(
            marker,
            marker
            + "              ! -name "
            "'test_adopt_git_permission_source_successor.py' \\\n",
            1,
        )
        failures: list[str] = []
        policy.validate_script_tests_budget(changed, failures)
        self.assertTrue(
            any(
                "must not exclude successor deployment contract tests"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_exact_b_job_steps_are_structurally_ordered(self) -> None:
        failures: list[str] = []
        policy.validate_exact_b_job(CI_TEXT, failures)
        self.assertEqual(failures, [])

        login = "Log in for the exact private B images"
        transition = (
            "Run real B-schema through F/0013, F/0014 and F/0015 transition smoke"
        )
        changed = CI_TEXT.replace(login, "TEMPORARY", 1)
        changed = changed.replace(transition, login, 1)
        changed = changed.replace("TEMPORARY", transition, 1)
        failures = []
        policy.validate_exact_b_job(changed, failures)
        self.assertTrue(
            any("missing or reorders" in failure for failure in failures),
            failures,
        )

    def test_postgres_client_bootstrap_is_fast_and_bounded(self) -> None:
        failures: list[str] = []
        policy.validate_postgres_client_bootstrap(
            CI_TEXT,
            POSTGRES_CLIENT_BOOTSTRAP,
            failures,
        )
        self.assertEqual(failures, [])

        controls = (
            '[[ -x "$POSTGRES_BIN/$tool" ]] || return 1',
            "readonly MAX_INSTALL_ATTEMPTS=3",
            "-o Acquire::Retries=2",
            "-o Acquire::http::Timeout=15",
            "-o Acquire::https::Timeout=15",
            "-o DPkg::Lock::Timeout=30",
            "/usr/bin/sudo --non-interactive",
            "/usr/bin/timeout --signal=TERM --kill-after=10s 60s",
            "/usr/bin/timeout --signal=TERM --kill-after=10s 90s",
            "/usr/bin/env DEBIAN_FRONTEND=noninteractive",
            '/usr/bin/sleep "$((attempt * 5))"',
        )
        for control in controls:
            with self.subTest(control=control):
                changed = POSTGRES_CLIENT_BOOTSTRAP.replace(
                    control.encode(),
                    b"removed-reviewed-control",
                    1,
                )
                failures = []
                policy.validate_postgres_client_bootstrap(
                    CI_TEXT,
                    changed,
                    failures,
                )
                self.assertTrue(failures)

    def test_postgres_client_bootstrap_cannot_be_moved_to_another_job(
        self,
    ) -> None:
        helper_line = "          scripts/ci/ensure_postgresql_16_client.sh"
        changed = CI_TEXT.replace(helper_line, "          true", 1)
        insertion = (
            "          go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7"
        )
        changed = changed.replace(
            insertion,
            insertion + "\n" + helper_line,
            1,
        )
        self.assertEqual(
            changed.count(helper_line),
            CI_TEXT.count(helper_line),
        )

        failures: list[str] = []
        policy.validate_postgres_client_bootstrap(
            changed,
            POSTGRES_CLIENT_BOOTSTRAP,
            failures,
        )
        self.assertTrue(
            any("production-alias-integration" in failure for failure in failures),
            failures,
        )

    def test_postgres_client_bootstrap_requires_each_step_timeout(self) -> None:
        changed = CI_TEXT.replace("        timeout-minutes: 9\n", "", 1)
        failures: list[str] = []
        policy.validate_postgres_client_bootstrap(
            changed,
            POSTGRES_CLIENT_BOOTSTRAP,
            failures,
        )
        self.assertTrue(
            any("exact active" in failure for failure in failures),
            failures,
        )

    def test_postgres_client_bootstrap_rejects_dead_code(self) -> None:
        changed_bootstrap = POSTGRES_CLIENT_BOOTSTRAP.replace(
            b"set -euo pipefail\n",
            b"set -euo pipefail\nexit 0\n",
            1,
        )
        failures: list[str] = []
        policy.validate_postgres_client_bootstrap(
            CI_TEXT,
            changed_bootstrap,
            failures,
        )
        self.assertTrue(failures)

        helper_line = "          scripts/ci/ensure_postgresql_16_client.sh"
        changed_workflow = CI_TEXT.replace(
            helper_line,
            "          exit 0\n" + helper_line,
            1,
        )
        failures = []
        policy.validate_postgres_client_bootstrap(
            changed_workflow,
            POSTGRES_CLIENT_BOOTSTRAP,
            failures,
        )
        self.assertTrue(failures)

    def test_postgres_client_bootstrap_rejects_alternate_duplicate(self) -> None:
        helper_line = "          scripts/ci/ensure_postgresql_16_client.sh"
        changed = CI_TEXT.replace(
            helper_line,
            helper_line
            + "\n          bash scripts/ci/ensure_postgresql_16_client.sh",
            1,
        )
        failures: list[str] = []
        policy.validate_postgres_client_bootstrap(
            changed,
            POSTGRES_CLIENT_BOOTSTRAP,
            failures,
        )
        self.assertTrue(failures)

    def test_postgres_client_bootstrap_rejects_commented_timeout(self) -> None:
        changed = CI_TEXT.replace(
            "        timeout-minutes: 9\n",
            "        # timeout-minutes: 9\n",
            1,
        )
        failures: list[str] = []
        policy.validate_postgres_client_bootstrap(
            changed,
            POSTGRES_CLIENT_BOOTSTRAP,
            failures,
        )
        self.assertTrue(failures)

    def test_gpu_session_compose_render_is_governed(self) -> None:
        failures: list[str] = []
        policy.validate_gpu_session_compose_policy(CI_TEXT, failures)
        self.assertEqual(failures, [])

    def test_gpu_session_compose_render_requires_exact_inputs(self) -> None:
        controls = (
            (
                'NEXPOLY_DEV_FRONTEND_PORT: "9001"',
                'NEXPOLY_DEV_FRONTEND_PORT: "15173"',
                "fixed 9001 development frontend port",
            ),
            (
                'NEXPOLY_DEV_GPU_SESSION_ID: "dddddddddddddddddddddddddddddddd"',
                'NEXPOLY_DEV_GPU_SESSION_ID: "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"',
                "fixed development GPU session identity",
            ),
            (
                "NEXPOLY_GPU_STATE_ROOT: /tmp/nexpoly-gpu-state",
                "NEXPOLY_GPU_STATE_ROOT: /tmp/other-gpu-state",
                "isolated development GPU state root",
            ),
            (
                "docker compose -f docker-compose.yml -f docker-compose.dev.yml "
                "-f docker-compose.dev-gpu-launcher.yml config --quiet",
                "docker compose -f docker-compose.yml -f docker-compose.dev.yml "
                "config --quiet",
                "9001-only GPU-launcher Compose render",
            ),
            (
                "docker compose -f docker-compose.yml -f docker-compose.dev.yml "
                "-f docker-compose.dev-gpu-session.yml config --quiet",
                "docker compose -f docker-compose.yml -f docker-compose.dev.yml "
                "config --quiet",
                "base, development, and GPU-session Compose render",
            ),
        )
        for marker, replacement, expected_failure in controls:
            with self.subTest(marker=marker):
                changed = CI_TEXT.replace(marker, replacement, 1)
                failures: list[str] = []
                policy.validate_gpu_session_compose_policy(changed, failures)
                self.assertTrue(
                    any(expected_failure in failure for failure in failures),
                    failures,
                )

    def test_gpu_session_compose_render_rejects_commented_controls(self) -> None:
        controls = (
            '          NEXPOLY_DEV_FRONTEND_PORT: "9001"',
            '          NEXPOLY_DEV_GPU_SESSION_ID: "dddddddddddddddddddddddddddddddd"',
            "          NEXPOLY_GPU_STATE_ROOT: /tmp/nexpoly-gpu-state",
            "          docker compose -f docker-compose.yml -f docker-compose.dev.yml "
            "-f docker-compose.dev-gpu-launcher.yml config --quiet",
            "          docker compose -f docker-compose.yml -f docker-compose.dev.yml "
            "-f docker-compose.dev-gpu-session.yml config --quiet",
        )
        changed = CI_TEXT
        for line in controls:
            changed = changed.replace(line, "          # " + line.strip(), 1)

        failures: list[str] = []
        policy.validate_gpu_session_compose_policy(changed, failures)

        self.assertTrue(
            any(
                "active fixed 9001 development frontend port" in failure
                for failure in failures
            ),
            failures,
        )
        self.assertTrue(
            any("active fixed development GPU session identity" in failure for failure in failures),
            failures,
        )
        self.assertTrue(
            any("active isolated development GPU state root" in failure for failure in failures),
            failures,
        )
        self.assertTrue(
            any("active base, development, and GPU-session Compose render" in failure for failure in failures),
            failures,
        )

    def test_gpu_session_compose_render_rejects_disabled_step(self) -> None:
        marker = "      - name: Validate Compose configurations\n"
        changed = CI_TEXT.replace(
            marker,
            marker + "        if: ${{ false }}\n",
            1,
        )

        failures: list[str] = []
        policy.validate_gpu_session_compose_policy(changed, failures)

        self.assertTrue(
            any("must not define an if condition" in failure for failure in failures),
            failures,
        )

    def test_backend_image_identity_is_structurally_governed(self) -> None:
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(CI_TEXT, failures)
        self.assertEqual(failures, [])

    def test_backend_image_identity_rejects_changed_hash_inputs(self) -> None:
        changed = CI_TEXT.replace(
            "              backend/requirements-legacy.lock \\\n",
            "              backend/requirements-system.lock \\\n",
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "image-build Backend image identity step must use the exact"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_identity_rejects_changed_hash_algorithm(self) -> None:
        changed = CI_TEXT.replace(
            "              sha256sum | awk '{print $1}'\n",
            "              md5sum | awk '{print $1}'\n",
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "image-build Backend image identity step must use the exact"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_identity_rejects_deleted_output(self) -> None:
        changed = CI_TEXT.replace(
            '            echo "build_config_sha256=${build_config_sha256}"\n',
            "",
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "image-build Backend image identity step must use the exact"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_identity_rejects_empty_output(self) -> None:
        changed = CI_TEXT.replace(
            '            echo "source_tree=${source_tree}"\n',
            '            echo "source_tree="\n',
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "image-build Backend image identity step must use the exact"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_release_backend_image_identity_rejects_deleted_output(self) -> None:
        marker = '            echo "build_config_sha256=${build_config_sha256}"\n'
        release_start = CI_TEXT.index("  release:\n")
        marker_start = CI_TEXT.index(marker, release_start)
        changed = CI_TEXT[:marker_start] + CI_TEXT[marker_start + len(marker) :]
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "release Backend image identity step must use the exact"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_identity_rejects_disabled_step(self) -> None:
        changed = CI_TEXT.replace(
            "        name: Resolve Backend image identity\n",
            "        name: Resolve Backend image identity\n"
            "        if: ${{ false }}\n",
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "image-build Backend image identity step must use the exact"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_identity_rejects_commented_step(self) -> None:
        changed = CI_TEXT.replace(
            "      - id: backend-identity\n",
            "      # - id: backend-identity\n",
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "image-build Backend image identity step must use the exact"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_identity_rejects_step_in_wrong_job(self) -> None:
        identity_step = policy.BACKEND_IMAGE_IDENTITY_STEP + "\n"
        changed = CI_TEXT.replace(identity_step, "", 1)
        policy_job = changed.index("  policy:\n")
        assertion = (
            '        run: scripts/ci/assert_candidate_sha.sh '
            '"${{ needs.resolve-sha.outputs.candidate_sha }}"\n'
        )
        insertion = changed.index(assertion, policy_job) + len(assertion)
        changed = changed[:insertion] + identity_step + changed[insertion:]
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "image-build must contain exactly one active Backend image "
                "identity step" in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_identity_rejects_step_after_build(self) -> None:
        identity_step = policy.BACKEND_IMAGE_IDENTITY_STEP + "\n"
        changed = CI_TEXT.replace(identity_step, "", 1)
        cache_marker = (
            "          cache-to: type=gha,mode=max,scope=ci-"
            "${{ matrix.cache_scope }}-${{ hashFiles(matrix.lock_pattern) }}\n"
        )
        changed = changed.replace(
            cache_marker,
            cache_marker + identity_step,
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "image-build Backend image identity must be immediately adjacent"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_identity_rejects_unbound_build_argument(self) -> None:
        changed = CI_TEXT.replace(
            (
                "            DEPENDENCY_LOCK_SHA256="
                "${{ steps.backend-identity.outputs.dependency_lock_sha256 }}\n"
            ),
            "            DEPENDENCY_LOCK_SHA256=unknown\n",
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "image-build Backend build must use the exact governed action"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_identity_rejects_build_condition(self) -> None:
        changed = CI_TEXT.replace(
            "      - name: Build image\n"
            "        uses: docker/build-push-action@",
            "      - name: Build image\n"
            "        if: ${{ matrix.name == 'web' }}\n"
            "        uses: docker/build-push-action@",
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "image-build Backend build must use the exact governed action"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_release_backend_identity_rejects_continue_on_error(self) -> None:
        changed = CI_TEXT.replace(
            "        name: Build and push Backend once\n"
            "        uses: docker/build-push-action@",
            "        name: Build and push Backend once\n"
            "        continue-on-error: true\n"
            "        uses: docker/build-push-action@",
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "release Backend build must use the exact governed action"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_identity_rejects_alternate_context(self) -> None:
        changed = CI_TEXT.replace(
            policy.REVIEWED_GIT_CONTEXT_LINE + "\n",
            "          context: ./alternate\n",
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "image-build Backend build must use the exact governed action"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_identity_rejects_hidden_context_injection(
        self,
    ) -> None:
        marker = (
            "      - name: Assert immutable checkout\n"
            "        run: scripts/ci/assert_candidate_sha.sh "
            '"${{ needs.resolve-sha.outputs.candidate_sha }}"\n'
        )
        image_job = CI_TEXT.index("  image-build:\n")
        insertion = CI_TEXT.index(marker, image_job) + len(marker)
        changed = (
            CI_TEXT[:insertion]
            + "      - name: Hide an unreviewed Backend source\n"
            "        run: |\n"
            "          printf 'payload\\n' > backend/app/unreviewed.py\n"
            "          printf 'backend/app/unreviewed.py\\n' >> .git/info/exclude\n"
            + CI_TEXT[insertion:]
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "exact reviewed step sequence" in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_identity_rejects_intervening_mutation(self) -> None:
        marker = "      - name: Build image\n"
        changed = CI_TEXT.replace(
            marker,
            "      - name: Mutate Backend build context\n"
            "        run: printf 'FROM scratch\\n' > Dockerfile\n"
            + marker,
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "image-build Backend image identity must be immediately adjacent"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_identity_rejects_disabled_verification(self) -> None:
        changed = CI_TEXT.replace(
            "      - name: Verify Backend image identity\n"
            "        if: matrix.name == 'backend'\n",
            "      - name: Verify Backend image identity\n"
            "        if: ${{ false }}\n",
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "image-build Backend image verification must inspect the exact"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_release_backend_identity_rejects_unbound_digest_inspection(
        self,
    ) -> None:
        changed = CI_TEXT.replace(
            (
                "          BACKEND_IMAGE: ghcr.io/lzq390/nexpoly-backend@"
                "${{ steps.backend.outputs.digest }}\n"
            ),
            "          BACKEND_IMAGE: ghcr.io/lzq390/nexpoly-backend:latest\n",
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "release Backend image verification must inspect the exact"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_release_rejects_tag_rewrite_after_final_digest_seal(
        self,
    ) -> None:
        seal = "\n".join(policy.RELEASE_TAG_SEAL_STEP_LINES) + "\n"
        changed = CI_TEXT.replace(
            seal,
            seal
            + "      - name: Rewrite reviewed SHA tag\n"
            "        run: |\n"
            "          image=ghcr.io/lzq390/nexpoly-backend\n"
            "          docker tag local:unreviewed "
            '"${image}:sha-${{ needs.resolve-sha.outputs.candidate_sha }}"\n'
            "          docker push "
            '"${image}:sha-${{ needs.resolve-sha.outputs.candidate_sha }}"\n',
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "exact reviewed step sequence" in failure
                or "digest seal must be the final step" in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_identity_rejects_changed_matrix_backend(
        self,
    ) -> None:
        changed = CI_TEXT.replace(
            "          - name: backend\n"
            "            file: Dockerfile\n",
            "          - name: backend\n"
            "            file: frontend/Dockerfile\n",
            1,
        )
        failures: list[str] = []
        policy.validate_backend_image_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "exact active Backend/Web matrix" in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_dockerfile_identity_is_structurally_governed(self) -> None:
        failures: list[str] = []
        policy.validate_backend_dockerfile_identity_policy(
            DOCKERFILE_TEXT,
            failures,
        )
        self.assertEqual(failures, [])

    def test_backend_dockerfile_rejects_unbound_label(self) -> None:
        changed = DOCKERFILE_TEXT.replace(
            '      com.nexpoly.source.tree="$SOURCE_TREE" \\\n',
            '      com.nexpoly.source.tree="unknown" \\\n',
            1,
        )
        failures: list[str] = []
        policy.validate_backend_dockerfile_identity_policy(changed, failures)
        self.assertTrue(
            any("ARG-to-LABEL-and-ENV" in failure for failure in failures),
            failures,
        )

    def test_backend_dockerfile_rejects_unbound_environment(self) -> None:
        changed = DOCKERFILE_TEXT.replace(
            "    BUILD_CONFIG_SHA256=${BUILD_CONFIG_SHA256}\n",
            "    BUILD_CONFIG_SHA256=unknown\n",
            1,
        )
        failures: list[str] = []
        policy.validate_backend_dockerfile_identity_policy(changed, failures)
        self.assertTrue(
            any("ARG-to-LABEL-and-ENV" in failure for failure in failures),
            failures,
        )

    def test_backend_dockerfile_rejects_late_identity_override(self) -> None:
        changed = DOCKERFILE_TEXT.replace(
            "WORKDIR /app/backend\n",
            "ENV BUILD_SOURCE_TREE=unknown\n\nWORKDIR /app/backend\n",
            1,
        )
        failures: list[str] = []
        policy.validate_backend_dockerfile_identity_policy(changed, failures)
        self.assertTrue(
            any(
                "must not override Backend image identity" in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_assertion_script_is_pinned(self) -> None:
        failures: list[str] = []
        policy.validate_backend_image_assertion_script(
            BACKEND_IMAGE_ASSERTION,
            failures,
        )
        self.assertEqual(failures, [])

        failures = []
        policy.validate_backend_image_assertion_script(
            BACKEND_IMAGE_ASSERTION + b"\nexit 0\n",
            failures,
        )
        self.assertTrue(
            any(
                "assertion script differs" in failure
                for failure in failures
            ),
            failures,
        )

    def test_backend_image_assertion_checks_labels_and_environment(self) -> None:
        revision = "a" * 40
        tree = "b" * 40
        dependency_lock = "sha256:" + "c" * 64
        build_config = "sha256:" + "d" * 64
        config = {
            "Labels": {
                "org.opencontainers.image.revision": revision,
                "com.nexpoly.source.tree": tree,
                "com.nexpoly.backend.dependency-lock": dependency_lock,
                "com.nexpoly.backend.build-config": build_config,
            },
            "Env": [
                f"BUILD_REVISION={revision}",
                f"BUILD_SOURCE_TREE={tree}",
                f"BUILD_DEPENDENCY_LOCK_SHA256={dependency_lock}",
                f"BUILD_CONFIG_SHA256={build_config}",
            ],
        }
        environment = dict(os.environ)
        environment["FAKE_BACKEND_CONFIG"] = json.dumps(
            config,
            separators=(",", ":"),
        )
        harness = (
            "docker() { printf '%s\\n' \"$FAKE_BACKEND_CONFIG\"; }\n"
            "export -f docker\n"
            f'exec "{BACKEND_IMAGE_ASSERTION_PATH}" "$@"\n'
        )
        arguments = (
            "fake:image",
            revision,
            tree,
            dependency_lock,
            build_config,
        )
        valid = subprocess.run(
            ("bash", "-c", harness, "identity-test", *arguments),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)

        invalid = subprocess.run(
            (
                "bash",
                "-c",
                harness,
                "identity-test",
                *arguments[:-1],
                "sha256:" + "e" * 64,
            ),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn(
            "label com.nexpoly.backend.build-config differs",
            invalid.stderr,
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

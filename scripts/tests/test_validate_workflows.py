from __future__ import annotations

import json
import os
from pathlib import Path
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
EXACT_B_TEXT = (ROOT / "scripts/ci/test_exact_b_bridge.sh").read_text(
    encoding="utf-8"
)


class StructuredWorkflowPolicyTests(unittest.TestCase):
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

    def test_exact_b_job_steps_are_structurally_ordered(self) -> None:
        failures: list[str] = []
        policy.validate_exact_b_job(CI_TEXT, failures)
        self.assertEqual(failures, [])

        login = "Log in for the exact private B images"
        transition = (
            "Run real B-schema through F/0013 and F/0014 transition smoke"
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

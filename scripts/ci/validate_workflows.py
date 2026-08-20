#!/usr/bin/env python3
"""Dependency-free policy checks for the single NexPoly CI/CD workflow."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import stat
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = REPOSITORY_ROOT / ".github" / "workflows"
CI_PATH = WORKFLOW_ROOT / "ci.yml"
RELEASE_INPUT_PATH = REPOSITORY_ROOT / "release-input.json"
DEPLOYMENT_DOC_PATH = REPOSITORY_ROOT / "docs" / "deployment.md"
RELEASE_CONTROLLER_DOC_PATH = (
    REPOSITORY_ROOT / "docs" / "release-controller.md"
)
SOURCE_SUCCESSOR_PATH = (
    REPOSITORY_ROOT / "scripts" / "adopt_git_permission_source_successor.py"
)
ADOPT_RUNTIME_PREREQUISITES_PATH = (
    REPOSITORY_ROOT / "scripts" / "adopt_runtime_prerequisites.py"
)
PULL_DEPLOY_CONTROLLER_PATH = (
    REPOSITORY_ROOT / "scripts" / "pull_deploy_controller.py"
)
LEGACY_REMOTE_RELEASE_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "remote_release.sh"
EXACT_B_BRIDGE_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "test_exact_b_bridge.sh"
BACKEND_DOCKERFILE_PATH = REPOSITORY_ROOT / "Dockerfile"
BACKEND_IMAGE_ASSERTION_PATH = (
    REPOSITORY_ROOT / "scripts" / "ci" / "assert_backend_image_identity.sh"
)
POSTGRES_CLIENT_BOOTSTRAP_PATH = (
    REPOSITORY_ROOT / "scripts" / "ci" / "ensure_postgresql_16_client.sh"
)
OPENSCIENCE_DOCKERFILE_PATH = (
    REPOSITORY_ROOT / "ops" / "openscience-ui-overlay" / "Dockerfile"
)
OPENSCIENCE_PATCH_PATH = (
    REPOSITORY_ROOT / "ops" / "openscience-ui-overlay" / "patch.mjs"
)
OPENSCIENCE_PACKAGE_LOCK_PATH = (
    REPOSITORY_ROOT / "ops" / "openscience-ui-overlay" / "package-lock.json"
)
OPENSCIENCE_BROWSER_PROBE_PATH = (
    REPOSITORY_ROOT / "ops" / "openscience-ui-overlay" / "browser_probe.mjs"
)
OPENSCIENCE_IMAGE_ASSERTION_PATH = (
    REPOSITORY_ROOT / "scripts" / "ci" / "test_openscience_overlay_image.sh"
)
OPENSCIENCE_BROWSER_ASSERTION_PATH = (
    REPOSITORY_ROOT / "scripts" / "ci" / "test_openscience_bridge_browser.sh"
)
OPENSCIENCE_RELEASE_CONTROLLER_PATH = (
    REPOSITORY_ROOT / "scripts" / "openscience_ui_release.py"
)

PINNED_ACTION = re.compile(
    r"^\s*-?\s*uses:\s*[^\s@]+@([0-9a-f]{40})(?:\s*#.*)?$"
)
ANY_ACTION = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)")
WORKFLOW_JOB_HEADER = re.compile(r"(?m)^  (?P<name>[A-Za-z0-9_-]+):\n")
WORKFLOW_STEP_ITEM = re.compile(r"(?m)^      - ")
WORKFLOW_STEP_KEY = re.compile(r"^        [A-Za-z_][A-Za-z0-9_-]*:")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_DATASET = re.compile(r"^[A-Za-z0-9_.-]+$")
FROZEN_ASSET_DOC = re.compile(
    r"The frozen schema-v2 asset manifest is\s+"
    r"`(sha256:[0-9a-f]{64})`"
)
EXPECTED_ASSET_DIGEST = (
    "sha256:0588cc6a9acd50efbcba49850bbea79ab44fa1752fa530b8537ccb21753ebc9b"
)
EXPECTED_PREDECESSOR_ASSET_DIGEST = (
    "sha256:ad19a4f1cb954b3ee6999b7157c798fd887ecd3fd7ae12e40ac20a97637575e2"
)
EXPECTED_POSTGRES_IMAGE = (
    "postgres:16-alpine@sha256:"
    "57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
)
EXPECTED_POSTGRES_CLIENT_BOOTSTRAP_SHA256 = (
    "ba87105271808a188f43c18868a9ddbbb97a2d350ff0ca66f85ba3269e3183e6"
)
EXPECTED_POSTGRES_AUDIT_IMAGES = (
    "docker.io/library/postgres@sha256:"
    "f1341c01408dc7278e9d365ed4f860cd3f87dd16b4464ac326fc0f422083a579",
    "docker.io/library/postgres@sha256:"
    "3d0f7584ed7d04e27fa050d6683a74746608faf21f202be78460d679cc56461f",
    "docker.io/library/postgres@sha256:"
    "57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777",
    "docker.io/library/postgres@sha256:"
    "9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15",
)
EXPECTED_SOURCE_SUCCESSOR_MANIFEST = (
    "ops/config/bootstrap-quiesce.example",
    "ops/config/bootstrap-status.example",
    "ops/config/bootstrap-resume-unchanged.example",
    "ops/config/bootstrap-rollback.example",
    "ops/config/bootstrap-active-jobs-probe.example",
    "ops/config/bootstrap-legacy-runtime-status.example",
    "ops/config/bootstrap-legacy-runtime-resume-unchanged.example",
    "ops/config/bootstrap-legacy-runtime-restore.example",
    "ops/config/deployment-mutable-data-audit.example",
    "ops/config/mutable-data-audit.pg_service.conf.example",
    "scripts/bootstrap_pull_deploy.py",
    "scripts/git_source_trust.py",
    "scripts/bridge_deploy_core.py",
)
EXPECTED_SOURCE_SUCCESSOR_CHANGED_PATHS = (
    "scripts/bootstrap_pull_deploy.py",
    "scripts/git_source_trust.py",
)
EXPECTED_ADOPTION_SUCCESSOR_LINEAGE_FIELDS = {
    "schema_version",
    "source_successor_authority_sha256",
    "source_successor_completed_journal_sha256",
    "unit_permission_authority_sha256",
    "unit_permission_completed_journal_sha256",
    "unit_permission_transaction_inventory_sha256",
    "production_git_snapshot_authority_sha256",
    "bootstrap_router_intent_sha256",
    "bootstrap_router_authority_sha256",
}
ORDINARY_DEPLOYMENT_COMMANDS = (
    "/usr/bin/python3 -I -B ./scripts/pull_deploy_controller.py plan \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$deploy_operation_id"',
    "nexpoly-pull-deploy prepare \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$deploy_operation_id"',
    "./scripts/production_postgres_rehearsal.py \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$deploy_operation_id" \\\n'
    "  --plan",
    "./scripts/production_postgres_rehearsal.py \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$deploy_operation_id" \\\n'
    "  --apply \\\n"
    "  --confirm-descriptor-sha256 sha256:<reviewed-descriptor-digest> \\\n"
    "  --confirm-source-system-identifier "
    "<reviewed-decimal-system-identifier> \\\n"
    "  --confirm-source-ledger-sha256 sha256:<reviewed-ledger-digest> \\\n"
    "  --confirm-source-property-records 615159 \\\n"
    "  --confirm-plan-sha256 sha256:<reviewed-plan-digest>",
    "nexpoly-pull-deploy apply \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$deploy_operation_id"',
)
SOURCE_SUCCESSOR_PLAN_COMMAND = (
    "./scripts/adopt_git_permission_source_successor.py plan \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$source_successor_operation_id"'
)
PRODUCTION_GIT_SNAPSHOT_PLAN_COMMAND = (
    "./scripts/production_git_snapshot.py plan \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$snapshot_operation_id"'
)
PRODUCTION_GIT_SNAPSHOT_APPLY_COMMAND = (
    "./scripts/production_git_snapshot.py apply \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$snapshot_operation_id" \\\n'
    "  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \\\n"
    "  --confirm-snapshot-impact-sha256 sha256:<reviewed-impact-digest>"
)
SOURCE_SUCCESSOR_APPLY_COMMAND = (
    "./scripts/adopt_git_permission_source_successor.py apply \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$source_successor_operation_id" \\\n'
    "  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \\\n"
    "  --confirm-source-successor-impact-sha256 "
    "sha256:<reviewed-impact-digest>"
)
SOURCE_SUCCESSOR_MAIN_FREEZE_MARKERS = (
    "The first-deployment authority chain is executable only in this order",
    "snapshot → source-successor → unit-permission → bootstrap-router",
    "production_git_snapshot.py",
    "adopt_bootstrap_router_successor.py",
    "verify-integrity",
    "discard every uncommitted reviewed plan whenever protected `main` changes",
    "Neither an operator waiver nor a manually copied `.git` directory",
    "cover every first-deployment Git mutation",
    "returns to the predecessor or begins a new operation",
    "restore the entire verified pre-prepare `.git` snapshot",
    "Resetting `HEAD` or deleting individual refs is not sufficient",
    "Freeze protected `main` before starting the snapshot `plan`",
    "until the first deployment has durably written current-state v3",
    "before any durable successor intent, discard the reviewed plan",
    "`authority-commit-intent` or the create-once authority is `completed`",
    "do not publish a second successor authority or change the sealed target",
    "separately reviewed compatibility-recovery procedure",
)
CONTROL_CHAIN_ONLY_COMMAND_MARKERS = (
    "These commands are production-authorized only for the single frozen target",
    "Do not start any mutating step until the immediately preceding authority",
    "The Worker-unit command remains prohibited until the snapshot and source-successor authorities are complete",
    "The ordinary deployment commands remain prohibited until the bootstrap-router authority is complete",
    "the exact required snapshot authority",
)
UNIT_PERMISSION_PLAN_COMMAND = (
    "./scripts/adopt_runtime_prerequisites.py unit-permission-plan \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$unit_permission_operation_id"'
)
UNIT_PERMISSION_APPLY_COMMAND = (
    "./scripts/adopt_runtime_prerequisites.py unit-permission-apply \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$unit_permission_operation_id" \\\n'
    "  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \\\n"
    "  --confirm-unit-permission-impact-sha256 "
    "sha256:<reviewed-impact-digest>"
)
UNIT_PERMISSION_ABORT_COMMAND = (
    "./scripts/adopt_runtime_prerequisites.py unit-permission-abort \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$unit_permission_operation_id" \\\n'
    "  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \\\n"
    "  --confirm-unit-permission-impact-sha256 "
    "sha256:<reviewed-impact-digest>"
)
BOOTSTRAP_ROUTER_PLAN_COMMAND = (
    "./scripts/adopt_bootstrap_router_successor.py plan \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$router_operation_id"'
)
BOOTSTRAP_ROUTER_APPLY_COMMAND = (
    "./scripts/adopt_bootstrap_router_successor.py apply \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$router_operation_id" \\\n'
    "  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \\\n"
    "  --confirm-router-successor-impact-sha256 "
    "sha256:<reviewed-impact-digest> \\\n"
    "  --confirm-snapshot-authority-sha256 "
    "sha256:<reviewed-snapshot-authority-digest> \\\n"
    "  --confirm-source-successor-authority-sha256 "
    "sha256:<reviewed-source-authority-digest> \\\n"
    "  --confirm-unit-permission-authority-sha256 "
    "sha256:<reviewed-unit-authority-digest> \\\n"
    "  --confirm-predecessor-selector-sha256 "
    "sha256:<reviewed-predecessor-selector-digest>"
)
MUTABLE_ROLE_PLAN_COMMAND = (
    "./scripts/provision_mutable_data_audit_role.py \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$role_operation_id" \\\n'
    "  --plan"
)
MUTABLE_ROLE_APPLY_COMMAND = (
    "./scripts/provision_mutable_data_audit_role.py \\\n"
    "  --sha <full-main-sha> \\\n"
    '  --operation-id "$role_operation_id" \\\n'
    "  --apply \\\n"
    "  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \\\n"
    "  --confirm-public-lo-acl-sha256 "
    "sha256:<reviewed-public-lo-impact-digest>"
)
SUCCESSOR_AUTHORITY_COMMANDS = (
    PRODUCTION_GIT_SNAPSHOT_PLAN_COMMAND,
    PRODUCTION_GIT_SNAPSHOT_APPLY_COMMAND,
    SOURCE_SUCCESSOR_PLAN_COMMAND,
    SOURCE_SUCCESSOR_APPLY_COMMAND,
    UNIT_PERMISSION_PLAN_COMMAND,
    UNIT_PERMISSION_APPLY_COMMAND,
    BOOTSTRAP_ROUTER_PLAN_COMMAND,
    BOOTSTRAP_ROUTER_APPLY_COMMAND,
)
DEPLOYMENT_SUCCESSOR_FIRST_DEPLOYMENT_COMMANDS = (
    *SUCCESSOR_AUTHORITY_COMMANDS,
    MUTABLE_ROLE_PLAN_COMMAND,
    MUTABLE_ROLE_APPLY_COMMAND,
    *ORDINARY_DEPLOYMENT_COMMANDS,
)
CONTROLLER_MUTABLE_ROLE_MARKER = (
    "provision_mutable_data_audit_role.py --plan/--apply"
)
CONTROLLER_SUCCESSOR_FIRST_DEPLOYMENT_COMMANDS = (
    *SUCCESSOR_AUTHORITY_COMMANDS,
    CONTROLLER_MUTABLE_ROLE_MARKER,
    *ORDINARY_DEPLOYMENT_COMMANDS,
)
EXPECTED_B_SHA = "82a69ddb42bcd5c4666b5bf038d02414bccc6dde"
EXPECTED_B_TREE = "44e4b4c398b7b84abdeb40bc02b885569aba4d8b"
EXPECTED_B_BRIDGE_CORE_BLOB = "15b8a1378d4100a5c74666344107bf00661fe34f"
EXPECTED_B_BACKEND_IMAGE = (
    "ghcr.io/lzq390/nexpoly-backend@sha256:"
    "ecd522706ce34b6aa444b30f1dee49e34e9c5ab1e4bca78b6037848facacd8c7"
)
EXPECTED_B_WEB_IMAGE = (
    "ghcr.io/lzq390/nexpoly-web@sha256:"
    "bc4a472c7eab5fc4b2f1e278567d9fc2551ac70e720ff06053c297c6829c18e0"
)
BACKEND_IMAGE_IDENTITY_STEP_LINES = (
    "      - id: backend-identity",
    "        name: Resolve Backend image identity",
    "        shell: bash",
    "        run: |",
    "          set -euo pipefail",
    (
        '          test "$(git rev-parse HEAD)" = '
        '"${{ needs.resolve-sha.outputs.candidate_sha }}"'
    ),
    '          test -z "$(git status --porcelain=v1 --untracked-files=all)"',
    "          source_tree=\"$(git rev-parse 'HEAD^{tree}')\"",
    "          dependency_lock_sha256=\"sha256:$(",
    "            sha256sum \\",
    "              backend/requirements.lock \\",
    "              backend/requirements-system.lock \\",
    "              backend/requirements-legacy.lock \\",
    "              backend/requirements-ci.lock |",
    "              sha256sum | awk '{print $1}'",
    "          )\"",
    "          build_config_sha256=\"sha256:$(",
    "            sha256sum \\",
    "              Dockerfile \\",
    "              docker-compose.yml \\",
    "              docker-compose.dev.yml \\",
    "              docker-compose.dev-gpu-launcher.yml \\",
    "              docker-compose.gpu-governed.yml \\",
    "              docker-compose.dev-gpu-session.yml |",
    "              sha256sum | awk '{print $1}'",
    "          )\"",
    "          {",
    '            echo "source_tree=${source_tree}"',
    '            echo "dependency_lock_sha256=${dependency_lock_sha256}"',
    '            echo "build_config_sha256=${build_config_sha256}"',
    '          } >>"$GITHUB_OUTPUT"',
)
BACKEND_IMAGE_IDENTITY_STEP = "\n".join(BACKEND_IMAGE_IDENTITY_STEP_LINES)
REVIEWED_GIT_CONTEXT_LINE = (
    '          context: "https://github.com/lzq390/ZhijuPoly.git#'
    '${{ needs.resolve-sha.outputs.candidate_sha }}"'
)
REVIEWED_GIT_TOKEN_LINE = "          github-token: ${{ github.token }}"
IMAGE_BUILD_JOB_PREFIX_LINES = (
    "    name: Build ${{ matrix.name }} image without publishing",
    "    if: github.event_name == 'pull_request'",
    "    needs: resolve-sha",
    "    runs-on: ubuntu-24.04",
    "    timeout-minutes: 90",
    "    strategy:",
    "      fail-fast: false",
    "      matrix:",
    "        include:",
    "          - name: backend",
    "            file: Dockerfile",
    "            cache_scope: backend",
    "            lock_pattern: backend/requirements*.lock",
    "          - name: web",
    "            file: frontend/Dockerfile",
    "            cache_scope: web",
    "            lock_pattern: frontend/package-lock.json",
    "    steps:",
)
IMAGE_BUILD_STEP_HEADERS = (
    "      - name: Check out candidate",
    "      - name: Assert immutable checkout",
    "      - name: Set up Buildx",
    "      - id: backend-identity",
    "      - name: Build image",
    "      - name: Verify Backend image identity",
)
IMAGE_BUILD_STEP_LINES = (
    "      - name: Build image",
    (
        "        uses: docker/build-push-action@"
        "10e90e3645eae34f1e60eeb005ba3a3d33f178e8 # v6"
    ),
    "        with:",
    REVIEWED_GIT_CONTEXT_LINE,
    REVIEWED_GIT_TOKEN_LINE,
    "          file: ${{ matrix.file }}",
    "          platforms: linux/amd64",
    "          push: false",
    "          load: true",
    (
        "          tags: nexpoly-ci-${{ matrix.cache_scope }}:"
        "sha-${{ needs.resolve-sha.outputs.candidate_sha }}"
    ),
    "          build-args: |",
    "            SOURCE_REVISION=${{ needs.resolve-sha.outputs.candidate_sha }}",
    "            SOURCE_TREE=${{ steps.backend-identity.outputs.source_tree }}",
    (
        "            DEPENDENCY_LOCK_SHA256="
        "${{ steps.backend-identity.outputs.dependency_lock_sha256 }}"
    ),
    (
        "            BUILD_CONFIG_SHA256="
        "${{ steps.backend-identity.outputs.build_config_sha256 }}"
    ),
    "            SOURCE_URL=${{ github.server_url }}/${{ github.repository }}",
    "            VERSION=sha-${{ needs.resolve-sha.outputs.candidate_sha }}",
    (
        "          cache-from: type=gha,scope=ci-${{ matrix.cache_scope }}-"
        "${{ hashFiles(matrix.lock_pattern) }}"
    ),
    (
        "          cache-to: type=gha,mode=max,scope=ci-"
        "${{ matrix.cache_scope }}-${{ hashFiles(matrix.lock_pattern) }}"
    ),
)
IMAGE_BUILD_VERIFY_STEP_LINES = (
    "      - name: Verify Backend image identity",
    "        if: matrix.name == 'backend'",
    "        env:",
    (
        "          BACKEND_IMAGE: nexpoly-ci-backend:"
        "sha-${{ needs.resolve-sha.outputs.candidate_sha }}"
    ),
    "          EXPECTED_REVISION: ${{ needs.resolve-sha.outputs.candidate_sha }}",
    "          EXPECTED_TREE: ${{ steps.backend-identity.outputs.source_tree }}",
    (
        "          EXPECTED_DEPENDENCY_LOCK: "
        "${{ steps.backend-identity.outputs.dependency_lock_sha256 }}"
    ),
    (
        "          EXPECTED_BUILD_CONFIG: "
        "${{ steps.backend-identity.outputs.build_config_sha256 }}"
    ),
    "        run: |",
    "          scripts/ci/assert_backend_image_identity.sh \\",
    '            "$BACKEND_IMAGE" \\',
    '            "$EXPECTED_REVISION" \\',
    '            "$EXPECTED_TREE" \\',
    '            "$EXPECTED_DEPENDENCY_LOCK" \\',
    '            "$EXPECTED_BUILD_CONFIG"',
)
RELEASE_BACKEND_BUILD_STEP_LINES = (
    "      - id: backend",
    "        name: Build and push Backend once",
    (
        "        uses: docker/build-push-action@"
        "10e90e3645eae34f1e60eeb005ba3a3d33f178e8 # v6"
    ),
    "        with:",
    REVIEWED_GIT_CONTEXT_LINE,
    REVIEWED_GIT_TOKEN_LINE,
    "          file: Dockerfile",
    "          platforms: linux/amd64",
    "          push: true",
    (
        "          tags: ghcr.io/lzq390/nexpoly-backend:"
        "sha-${{ needs.resolve-sha.outputs.candidate_sha }}"
    ),
    "          labels: |",
    (
        "            org.opencontainers.image.revision="
        "${{ needs.resolve-sha.outputs.candidate_sha }}"
    ),
    (
        "            org.opencontainers.image.source="
        "${{ github.server_url }}/${{ github.repository }}"
    ),
    (
        "            org.opencontainers.image.version="
        "sha-${{ needs.resolve-sha.outputs.candidate_sha }}"
    ),
    "          build-args: |",
    "            SOURCE_REVISION=${{ needs.resolve-sha.outputs.candidate_sha }}",
    "            SOURCE_TREE=${{ steps.backend-identity.outputs.source_tree }}",
    (
        "            DEPENDENCY_LOCK_SHA256="
        "${{ steps.backend-identity.outputs.dependency_lock_sha256 }}"
    ),
    (
        "            BUILD_CONFIG_SHA256="
        "${{ steps.backend-identity.outputs.build_config_sha256 }}"
    ),
    "            SOURCE_URL=${{ github.server_url }}/${{ github.repository }}",
    "            VERSION=sha-${{ needs.resolve-sha.outputs.candidate_sha }}",
    (
        "          cache-from: type=gha,scope=release-backend-"
        "${{ hashFiles('backend/requirements*.lock') }}"
    ),
    (
        "          cache-to: type=gha,mode=max,scope=release-backend-"
        "${{ hashFiles('backend/requirements*.lock') }}"
    ),
    "          provenance: false",
)
RELEASE_BACKEND_VERIFY_STEP_LINES = (
    "      - name: Verify published Backend image identity",
    "        env:",
    (
        "          BACKEND_IMAGE: ghcr.io/lzq390/nexpoly-backend@"
        "${{ steps.backend.outputs.digest }}"
    ),
    "          EXPECTED_REVISION: ${{ needs.resolve-sha.outputs.candidate_sha }}",
    "          EXPECTED_TREE: ${{ steps.backend-identity.outputs.source_tree }}",
    (
        "          EXPECTED_DEPENDENCY_LOCK: "
        "${{ steps.backend-identity.outputs.dependency_lock_sha256 }}"
    ),
    (
        "          EXPECTED_BUILD_CONFIG: "
        "${{ steps.backend-identity.outputs.build_config_sha256 }}"
    ),
    "        run: |",
    '          docker pull "$BACKEND_IMAGE"',
    "          scripts/ci/assert_backend_image_identity.sh \\",
    '            "$BACKEND_IMAGE" \\',
    '            "$EXPECTED_REVISION" \\',
    '            "$EXPECTED_TREE" \\',
    '            "$EXPECTED_DEPENDENCY_LOCK" \\',
    '            "$EXPECTED_BUILD_CONFIG"',
)
RELEASE_WEB_BUILD_STEP_LINES = (
    "      - id: web",
    "        name: Build and push Web once",
    (
        "        uses: docker/build-push-action@"
        "10e90e3645eae34f1e60eeb005ba3a3d33f178e8 # v6"
    ),
    "        with:",
    REVIEWED_GIT_CONTEXT_LINE,
    REVIEWED_GIT_TOKEN_LINE,
    "          file: frontend/Dockerfile",
    "          platforms: linux/amd64",
    "          push: true",
    (
        "          tags: ghcr.io/lzq390/nexpoly-web:"
        "sha-${{ needs.resolve-sha.outputs.candidate_sha }}"
    ),
    "          labels: |",
    (
        "            org.opencontainers.image.revision="
        "${{ needs.resolve-sha.outputs.candidate_sha }}"
    ),
    (
        "            org.opencontainers.image.source="
        "${{ github.server_url }}/${{ github.repository }}"
    ),
    (
        "            org.opencontainers.image.version="
        "sha-${{ needs.resolve-sha.outputs.candidate_sha }}"
    ),
    "          build-args: |",
    "            SOURCE_REVISION=${{ needs.resolve-sha.outputs.candidate_sha }}",
    "            SOURCE_URL=${{ github.server_url }}/${{ github.repository }}",
    "            VERSION=sha-${{ needs.resolve-sha.outputs.candidate_sha }}",
    "            VITE_AGENT_WORKSPACE_URL=http://114.214.255.154:9011/",
    (
        "          cache-from: type=gha,scope=release-web-"
        "${{ hashFiles('frontend/package-lock.json') }}"
    ),
    (
        "          cache-to: type=gha,mode=max,scope=release-web-"
        "${{ hashFiles('frontend/package-lock.json') }}"
    ),
    "          provenance: false",
)
RELEASE_TAG_SEAL_STEP_LINES = (
    "      - name: Seal published SHA tags to reviewed digests",
    "        env:",
    "          RELEASE_COMMIT: ${{ needs.resolve-sha.outputs.candidate_sha }}",
    "          EXPECTED_BACKEND_DIGEST: ${{ steps.backend.outputs.digest }}",
    "          EXPECTED_WEB_DIGEST: ${{ steps.web.outputs.digest }}",
    "        run: |",
    "          set -euo pipefail",
    '          [[ "$RELEASE_COMMIT" =~ ^[0-9a-f]{40}$ ]]',
    (
        '          [[ "$EXPECTED_BACKEND_DIGEST" =~ '
        "^sha256:[0-9a-f]{64}$ ]]"
    ),
    (
        '          [[ "$EXPECTED_WEB_DIGEST" =~ '
        "^sha256:[0-9a-f]{64}$ ]]"
    ),
    "          resolve_remote_digest() {",
    '            local reference="$1"',
    "            local hash",
    '            hash="$(',
    (
        '              docker buildx imagetools inspect --raw "$reference" |'
    ),
    "                sha256sum | awk '{print $1}'",
    '            )"',
    '            [[ "$hash" =~ ^[0-9a-f]{64}$ ]]',
    "            printf 'sha256:%s\\n' \"$hash\"",
    "          }",
    (
        '          backend_tag="ghcr.io/lzq390/nexpoly-backend:'
        'sha-${RELEASE_COMMIT}"'
    ),
    (
        '          web_tag="ghcr.io/lzq390/nexpoly-web:'
        'sha-${RELEASE_COMMIT}"'
    ),
    '          test "$(resolve_remote_digest "$backend_tag")" = \\',
    '            "$EXPECTED_BACKEND_DIGEST"',
    '          test "$(resolve_remote_digest "$web_tag")" = \\',
    '            "$EXPECTED_WEB_DIGEST"',
)
RELEASE_STEP_HEADERS = (
    "      - name: Check out release SHA",
    "      - name: Assert immutable checkout",
    "      - name: Validate reviewed image publication policy",
    "      - name: Set up Buildx",
    "      - name: Log in to private GHCR",
    "      - id: backend-identity",
    "      - id: backend",
    "      - name: Verify published Backend image identity",
    "      - id: web",
    "      - name: Record immutable image references",
    "      - name: Smoke the exact published image digests",
    "      - name: Seal published SHA tags to reviewed digests",
)
OPENSCIENCE_OVERLAY_STEP_HEADERS = (
    "      - name: Check out candidate",
    "      - name: Assert immutable checkout",
    "      - name: Set up Node.js",
    "      - name: Install the pinned browser probe package",
    "      - name: Run deterministic overlay patch tests",
    "      - name: Set up Buildx",
    "      - name: Build the governed overlay without publishing",
    "      - name: Verify the governed overlay image",
    "      - name: Verify both trusted parents and rejected browser bridge cases",
)
OPENSCIENCE_RELEASE_STEP_HEADERS = (
    "      - name: Check out release SHA",
    "      - name: Assert immutable checkout",
    "      - name: Set up Buildx",
    "      - name: Log in to private GHCR",
    "      - id: openscience",
    "      - name: Verify the exact published OpenScience UI digest",
    "      - name: Seal the published OpenScience SHA tag",
)
BACKEND_DOCKERFILE_IDENTITY_LINES = (
    'ARG SOURCE_REVISION="unknown"',
    'ARG SOURCE_TREE="unknown"',
    'ARG DEPENDENCY_LOCK_SHA256="unknown"',
    'ARG BUILD_CONFIG_SHA256="unknown"',
    'ARG SOURCE_URL="https://github.com/lzq390/ZhijuPoly"',
    'ARG VERSION="dev"',
    "",
    'LABEL org.opencontainers.image.source="$SOURCE_URL" \\',
    '      org.opencontainers.image.revision="$SOURCE_REVISION" \\',
    '      com.nexpoly.source.tree="$SOURCE_TREE" \\',
    '      com.nexpoly.backend.dependency-lock="$DEPENDENCY_LOCK_SHA256" \\',
    '      com.nexpoly.backend.build-config="$BUILD_CONFIG_SHA256" \\',
    '      org.opencontainers.image.version="$VERSION"',
    "",
    "ENV BUILD_REVISION=${SOURCE_REVISION} \\",
    "    BUILD_SOURCE_TREE=${SOURCE_TREE} \\",
    "    BUILD_DEPENDENCY_LOCK_SHA256=${DEPENDENCY_LOCK_SHA256} \\",
    "    BUILD_CONFIG_SHA256=${BUILD_CONFIG_SHA256}",
)
BACKEND_DOCKERFILE_IDENTITY_BLOCK = "\n".join(
    BACKEND_DOCKERFILE_IDENTITY_LINES
)
BACKEND_DOCKERFILE_IDENTITY_TOKENS = (
    "SOURCE_REVISION",
    "SOURCE_TREE",
    "DEPENDENCY_LOCK_SHA256",
    "BUILD_CONFIG_SHA256",
    "com.nexpoly.source.tree",
    "com.nexpoly.backend.dependency-lock",
    "com.nexpoly.backend.build-config",
)
EXPECTED_BACKEND_IMAGE_ASSERTION_SHA256 = (
    "1b9877203bacfbeb890c200b88b2df665d66b482fd3dc516cf6d7fe4cb93c05c"
)


def require_markers(text: str, markers: tuple[str, ...], failures: list[str]) -> None:
    for marker in markers:
        if marker not in text:
            failures.append(f"ci.yml is missing required control: {marker}")


def require_ordered_markers(
    text: str,
    markers: tuple[tuple[str, str], ...],
    *,
    label: str,
    failures: list[str],
) -> None:
    cursor = 0
    for marker_label, marker in markers:
        position = text.find(marker, cursor)
        if position < 0:
            failures.append(
                f"{label} is missing or reorders required control: {marker_label}"
            )
            return
        cursor = position + len(marker)


def workflow_job_body(
    ci_text: str,
    job_name: str,
    failures: list[str],
) -> str | None:
    headers = list(WORKFLOW_JOB_HEADER.finditer(ci_text))
    matches: list[str] = []
    for index, header in enumerate(headers):
        if header.group("name") != job_name:
            continue
        end = headers[index + 1].start() if index + 1 < len(headers) else len(ci_text)
        matches.append(ci_text[header.end() : end])
    if len(matches) != 1:
        failures.append(
            f"ci.yml must contain exactly one structured {job_name} job; "
            f"found {len(matches)}"
        )
        return None
    return matches[0]


def workflow_step_blocks(job_body: str) -> list[str]:
    starts = list(WORKFLOW_STEP_ITEM.finditer(job_body))
    return [
        job_body[
            start.start() : (
                starts[index + 1].start()
                if index + 1 < len(starts)
                else len(job_body)
            )
        ]
        for index, start in enumerate(starts)
    ]


def validate_backend_image_identity_policy(
    ci_text: str,
    failures: list[str],
) -> None:
    jobs = (
        (
            "image-build",
            IMAGE_BUILD_STEP_LINES,
            IMAGE_BUILD_VERIFY_STEP_LINES,
        ),
        (
            "release",
            RELEASE_BACKEND_BUILD_STEP_LINES,
            RELEASE_BACKEND_VERIFY_STEP_LINES,
        ),
    )
    for job_name, expected_build_lines, expected_verify_lines in jobs:
        body = workflow_job_body(ci_text, job_name, failures)
        if body is None:
            continue
        if job_name == "image-build":
            first_step = WORKFLOW_STEP_ITEM.search(body)
            actual_prefix = (
                []
                if first_step is None
                else body[: first_step.start()].rstrip().splitlines()
            )
            if actual_prefix != list(IMAGE_BUILD_JOB_PREFIX_LINES):
                failures.append(
                    "image-build must retain the exact active Backend/Web "
                    "matrix and pull-request execution policy"
                )
        steps = workflow_step_blocks(body)
        expected_headers = (
            IMAGE_BUILD_STEP_HEADERS
            if job_name == "image-build"
            else RELEASE_STEP_HEADERS
        )
        actual_headers = tuple(
            step.splitlines()[0]
            for step in steps
            if step.splitlines()
        )
        if actual_headers != expected_headers:
            failures.append(
                f"{job_name} must retain the exact reviewed step sequence "
                "without injected build-context or registry mutations"
            )
        identity_indices = [
            index
            for index, step in enumerate(steps)
            if (
                "id: backend-identity" in step
                or "name: Resolve Backend image identity" in step
            )
        ]
        if len(identity_indices) != 1:
            failures.append(
                f"{job_name} must contain exactly one active Backend image "
                f"identity step; found {len(identity_indices)}"
            )
            continue
        identity_index = identity_indices[0]
        identity_step = steps[identity_index]
        if identity_step.splitlines() != list(BACKEND_IMAGE_IDENTITY_STEP_LINES):
            failures.append(
                f"{job_name} Backend image identity step must use the exact "
                "source-tree, dependency-lock, build-config, and output algorithm"
            )

        checkout_indices = [
            index
            for index, step in enumerate(steps)
            if "uses: actions/checkout@" in step
        ]
        assertion_indices = [
            index
            for index, step in enumerate(steps)
            if "name: Assert immutable checkout" in step
        ]
        build_indices = [
            index
            for index, step in enumerate(steps)
            if step.startswith(expected_build_lines[0] + "\n")
            or step.rstrip() == expected_build_lines[0]
        ]
        verify_indices = [
            index
            for index, step in enumerate(steps)
            if step.startswith(expected_verify_lines[0] + "\n")
            or step.rstrip() == expected_verify_lines[0]
        ]
        if (
            len(checkout_indices) != 1
            or len(assertion_indices) != 1
            or len(build_indices) != 1
            or len(verify_indices) != 1
        ):
            failures.append(
                f"{job_name} Backend identity policy requires one checkout, "
                "immutable assertion, Backend build, and image verification step"
            )
            continue
        checkout_index = checkout_indices[0]
        assertion_index = assertion_indices[0]
        build_index = build_indices[0]
        verify_index = verify_indices[0]
        if not checkout_index < assertion_index < identity_index:
            failures.append(
                f"{job_name} Backend image identity must run after checkout and "
                "its immutable assertion"
            )
        if build_index != identity_index + 1:
            failures.append(
                f"{job_name} Backend image identity must be immediately "
                "adjacent to the Backend build"
            )
        if verify_index != build_index + 1:
            failures.append(
                f"{job_name} Backend image verification must immediately "
                "follow the Backend build"
            )
        build_step = steps[build_index]
        if build_step.rstrip().splitlines() != list(expected_build_lines):
            failures.append(
                f"{job_name} Backend build must use the exact governed action, "
                "context, Dockerfile, platform, publication, cache, and "
                "immutable build arguments without a condition or "
                "continue-on-error"
            )
        verify_step = steps[verify_index]
        if verify_step.rstrip().splitlines() != list(expected_verify_lines):
            failures.append(
                f"{job_name} Backend image verification must inspect the exact "
                "built or published image identity"
            )
        if job_name == "release":
            web_indices = [
                index
                for index, step in enumerate(steps)
                if step.startswith(RELEASE_WEB_BUILD_STEP_LINES[0] + "\n")
                or step.rstrip() == RELEASE_WEB_BUILD_STEP_LINES[0]
            ]
            seal_indices = [
                index
                for index, step in enumerate(steps)
                if step.startswith(RELEASE_TAG_SEAL_STEP_LINES[0] + "\n")
                or step.rstrip() == RELEASE_TAG_SEAL_STEP_LINES[0]
            ]
            if len(web_indices) != 1:
                failures.append(
                    "release must contain one exact governed Web image build"
                )
            elif (
                steps[web_indices[0]].rstrip().splitlines()
                != list(RELEASE_WEB_BUILD_STEP_LINES)
            ):
                failures.append(
                    "release Web build must use the exact reviewed Git context, "
                    "action, SHA tag, labels, cache, and build arguments"
                )
            if len(seal_indices) != 1:
                failures.append(
                    "release must contain one final SHA-tag digest seal"
                )
            else:
                seal_index = seal_indices[0]
                if seal_index != len(steps) - 1:
                    failures.append(
                        "release SHA-tag digest seal must be the final step"
                    )
                if (
                    steps[seal_index].rstrip().splitlines()
                    != list(RELEASE_TAG_SEAL_STEP_LINES)
                ):
                    failures.append(
                        "release SHA-tag digest seal must resolve both published "
                        "tags and match the reviewed action digests"
                    )


def validate_openscience_release_policy(
    ci_text: str,
    failures: list[str],
) -> None:
    file_modes = {
        OPENSCIENCE_DOCKERFILE_PATH: 0o644,
        OPENSCIENCE_PATCH_PATH: 0o755,
        OPENSCIENCE_PACKAGE_LOCK_PATH: 0o644,
        OPENSCIENCE_BROWSER_PROBE_PATH: 0o755,
        OPENSCIENCE_IMAGE_ASSERTION_PATH: 0o755,
        OPENSCIENCE_BROWSER_ASSERTION_PATH: 0o755,
        OPENSCIENCE_RELEASE_CONTROLLER_PATH: 0o755,
    }
    payloads: dict[Path, str] = {}
    for path, expected_mode in file_modes.items():
        if path.is_symlink() or not path.is_file():
            failures.append(
                f"OpenScience release control is missing or unsafe: "
                f"{path.relative_to(REPOSITORY_ROOT)}"
            )
            continue
        actual_mode = stat.S_IMODE(path.stat().st_mode)
        if actual_mode != expected_mode:
            failures.append(
                f"{path.relative_to(REPOSITORY_ROOT)} must use mode "
                f"{expected_mode:04o}"
            )
        try:
            payloads[path] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            failures.append(f"OpenScience release control cannot be read: {exc}")

    dockerfile = payloads.get(OPENSCIENCE_DOCKERFILE_PATH, "")
    patch = payloads.get(OPENSCIENCE_PATCH_PATH, "")
    browser = payloads.get(OPENSCIENCE_BROWSER_PROBE_PATH, "")
    image_assertion = payloads.get(OPENSCIENCE_IMAGE_ASSERTION_PATH, "")
    browser_assertion = payloads.get(OPENSCIENCE_BROWSER_ASSERTION_PATH, "")
    release_controller = payloads.get(OPENSCIENCE_RELEASE_CONTROLLER_PATH, "")
    base_digest = (
        "sha256:e7d25a1b6d515daec641c8de9c98265f275991eee2396dc578ce9c2fcfdeb197"
    )
    parent_policy = (
        "sha256:955ae6f5f3d0710dcaacc0906f6326a4ba99321a0e47fc928c198c8967dd0042"
    )
    derived_tree = (
        "sha256:3810ec7d6428a960c14b305d5925a22dd03769c9ab36c091a7a387b7b82e3969"
    )
    dockerfile_markers = (
        f"ghcr.io/lzq390/nexpoly-web@{base_digest}",
        "FROM ${OPENSCIENCE_BASE_IMAGE} AS openscience-base",
        "FROM node:22-bookworm-slim@sha256:",
        "FROM ${OPENSCIENCE_BASE_IMAGE}",
        f'com.nexpoly.openscience.derived-static-tree="{derived_tree}"',
        f'com.nexpoly.openscience.parent-policy-sha256="{parent_policy}"',
        'USER root',
        'USER nginx',
    )
    for marker in dockerfile_markers:
        if marker not in dockerfile:
            failures.append(f"OpenScience Dockerfile is missing: {marker}")
    if dockerfile.count("FROM ${OPENSCIENCE_BASE_IMAGE}") != 2:
        failures.append("OpenScience Dockerfile must use the exact base manifest twice")
    if ":latest" in dockerfile or "provenance: true" in dockerfile:
        failures.append("OpenScience Dockerfile contains a mutable image control")

    patch_markers = (
        "BASE_BUNDLE_SHA256",
        "BASE_STATIC_TREE_SHA256",
        "PATCHED_STATIC_TREE_SHA256",
        "OLD_RESOLVER",
        "OLD_CALL",
        "document.referrer",
        "http://114.214.255.154:9000",
        "http://114.214.255.154:9001",
        derived_tree.removeprefix("sha256:"),
        parent_policy.removeprefix("sha256:"),
        'replaceExactly(source, OLD_RESOLVER, NEW_RESOLVER, 1, "bridge resolver")',
        "OpenScience trusted-parent call count differs",
    )
    for marker in patch_markers:
        if marker not in patch:
            failures.append(f"OpenScience deterministic patch is missing: {marker}")
    if '"*"' in patch or "'*'" in patch:
        failures.append("OpenScience deterministic patch must not contain a wildcard Origin")

    for marker in (
        "projects.snapshot",
        "general.sessions.snapshot",
        "other.namespace",
        "no-referrer",
        "sendFromSibling",
        "startBrowserProxy",
        "REVIEWED_PORTS",
    ):
        if marker not in browser:
            failures.append(f"OpenScience browser probe is missing: {marker}")
    for marker in (
        base_digest,
        derived_tree.removeprefix("sha256:"),
        parent_policy.removeprefix("sha256:"),
        "docker export",
        "base-rootfs-metadata",
    ):
        if marker not in image_assertion:
            failures.append(f"OpenScience image assertion is missing: {marker}")
    for marker in (
        "mcr.microsoft.com/playwright@sha256:",
        "--network \"container:$CANDIDATE_CONTAINER\"",
        "node ./browser_probe.mjs",
    ):
        if marker not in browser_assertion:
            failures.append(f"OpenScience browser assertion is missing: {marker}")
    for marker in (
        'commands.add_parser("plan")',
        'commands.add_parser("apply")',
        'commands.add_parser("rollback")',
        "run_browser_probe(name)",
        "run_browser_probe(LIVE_CONTAINER)",
        "OpenScience .env changed during candidate verification",
        "OpenScience release failed and was rolled back",
    ):
        if marker not in release_controller:
            failures.append(f"OpenScience release controller is missing: {marker}")

    package_lock_text = payloads.get(OPENSCIENCE_PACKAGE_LOCK_PATH)
    if package_lock_text is not None:
        try:
            package_lock = json.loads(package_lock_text)
            packages = package_lock.get("packages", {})
            root = packages.get("", {})
            playwright = packages.get("node_modules/playwright", {})
            playwright_core = packages.get("node_modules/playwright-core", {})
            if (
                package_lock.get("lockfileVersion") != 3
                or root.get("dependencies", {}).get("playwright") != "1.62.1"
                or playwright.get("version") != "1.62.1"
                or playwright_core.get("version") != "1.62.1"
                or not isinstance(playwright.get("integrity"), str)
                or not isinstance(playwright_core.get("integrity"), str)
            ):
                failures.append("OpenScience Playwright package lock is not exact")
        except json.JSONDecodeError as exc:
            failures.append(f"OpenScience Playwright package lock is invalid: {exc}")

    jobs = (
        ("openscience-overlay", OPENSCIENCE_OVERLAY_STEP_HEADERS),
        ("openscience-release", OPENSCIENCE_RELEASE_STEP_HEADERS),
    )
    for job_name, expected_headers in jobs:
        body = workflow_job_body(ci_text, job_name, failures)
        if body is None:
            continue
        steps = workflow_step_blocks(body)
        actual_headers = tuple(
            step.splitlines()[0] for step in steps if step.splitlines()
        )
        if actual_headers != expected_headers:
            failures.append(
                f"{job_name} must retain the exact reviewed step sequence"
            )
        if "continue-on-error:" in body or "pull_request_target" in body:
            failures.append(f"{job_name} contains a fail-open execution control")

    overlay_job = workflow_job_body(ci_text, "openscience-overlay", failures)
    if overlay_job is not None:
        for marker in (
            "    needs: resolve-sha",
            "    timeout-minutes: 30",
            "npm ci --ignore-scripts --prefix ops/openscience-ui-overlay",
            "push: false",
            "load: true",
            "provenance: false",
            "scripts/ci/test_openscience_overlay_image.sh",
            "scripts/ci/test_openscience_bridge_browser.sh",
        ):
            if marker not in overlay_job:
                failures.append(f"openscience-overlay job is missing: {marker}")
    release_job = workflow_job_body(ci_text, "openscience-release", failures)
    if release_job is not None:
        for marker in (
            "needs.ci-gate.result == 'success'",
            "github.event_name == 'push'",
            "    needs: [resolve-sha, ci-gate]",
            "      packages: write",
            "push: true",
            "provenance: false",
            "ghcr.io/lzq390/openscience-ui:sha-",
            "ghcr.io/lzq390/openscience-ui@${{ steps.openscience.outputs.digest }}",
            'test "$remote_digest" = "$EXPECTED_OPENSCIENCE_DIGEST"',
        ):
            if marker not in release_job:
                failures.append(f"openscience-release job is missing: {marker}")
    gate_job = workflow_job_body(ci_text, "ci-gate", failures)
    if gate_job is not None and gate_job.count("      - openscience-overlay") != 1:
        failures.append("ci-gate must require the governed OpenScience overlay job")


def validate_backend_dockerfile_identity_policy(
    dockerfile_text: str,
    failures: list[str],
) -> None:
    if dockerfile_text.count(BACKEND_DOCKERFILE_IDENTITY_BLOCK) != 1:
        failures.append(
            "Dockerfile must contain one exact Backend ARG-to-LABEL-and-ENV "
            "identity binding"
        )
        return
    remaining = dockerfile_text.replace(
        BACKEND_DOCKERFILE_IDENTITY_BLOCK,
        "",
        1,
    )
    unexpected = [
        token
        for token in BACKEND_DOCKERFILE_IDENTITY_TOKENS
        if token in remaining
    ]
    if unexpected:
        failures.append(
            "Dockerfile must not override Backend image identity outside its "
            "governed binding: "
            + ", ".join(unexpected)
        )


def validate_backend_image_assertion_script(
    script: bytes,
    failures: list[str],
) -> None:
    digest = hashlib.sha256(script).hexdigest()
    if digest != EXPECTED_BACKEND_IMAGE_ASSERTION_SHA256:
        failures.append(
            "Backend image identity assertion script differs from its reviewed "
            "label and environment verifier"
        )


def validate_complete_history_checkouts(
    ci_text: str,
    failures: list[str],
) -> None:
    jobs = (
        "script-tests",
        "production-alias-integration",
        "bridge-validation",
        "exact-b-bridge",
    )
    checkout = "uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
    candidate_ref = "ref: ${{ needs.resolve-sha.outputs.candidate_sha }}"
    for job_name in jobs:
        body = workflow_job_body(ci_text, job_name, failures)
        if body is None:
            continue
        if (
            body.count(checkout) != 1
            or body.count(candidate_ref) != 1
            or body.count("fetch-depth: 0") != 1
            or body.count("persist-credentials: false") != 1
            or body.count("Assert immutable checkout") != 1
        ):
            failures.append(
                f"{job_name} must have one exact candidate checkout with complete "
                "history, disabled credentials, and an immutable-SHA assertion"
            )
            continue
        require_ordered_markers(
            body,
            (
                ("pinned checkout", checkout),
                ("resolved candidate ref", candidate_ref),
                ("complete history", "fetch-depth: 0"),
                ("disabled credentials", "persist-credentials: false"),
                ("immutable checkout assertion", "Assert immutable checkout"),
            ),
            label=f"{job_name} checkout",
            failures=failures,
        )
    if ci_text.count("fetch-depth: 0") != len(jobs):
        failures.append(
            "only script-tests, production-alias-integration, bridge-validation, "
            "and exact-b-bridge may request complete Git history"
        )


def validate_script_tests_budget(
    ci_text: str,
    failures: list[str],
) -> None:
    body = workflow_job_body(ci_text, "script-tests", failures)
    if body is None:
        return
    if body.count("    timeout-minutes: 40\n") != 1:
        failures.append(
            "script-tests must retain its reviewed 40-minute fault-injection "
            "test budget"
        )
    protected_tests = (
        "test_production_git_snapshot.py",
        "test_restore_production_git_snapshot.py",
        "test_adopt_bootstrap_router_successor.py",
        "test_adopt_git_permission_source_successor.py",
        "test_adopt_runtime_prerequisites.py",
        "test_production_repository_transition.py",
        "test_pull_deploy_controller.py",
        "test_validate_workflows.py",
    )
    excluded = [
        name for name in protected_tests if f"! -name '{name}'" in body
    ]
    if excluded:
        failures.append(
            "script-tests must not exclude successor deployment contract tests: "
            + ", ".join(excluded)
        )


def validate_gpu_session_compose_policy(
    ci_text: str,
    failures: list[str],
) -> None:
    body = workflow_job_body(ci_text, "policy", failures)
    if body is None:
        return
    step_header = "      - name: Validate Compose configurations\n"
    if body.count(step_header) != 1:
        failures.append(
            "policy job must contain exactly one active Validate Compose "
            "configurations step"
        )
        return
    step_start = body.index(step_header)
    next_step = WORKFLOW_STEP_ITEM.search(body, step_start + len(step_header))
    step = body[step_start : next_step.start() if next_step else len(body)]
    step_lines = step.splitlines()
    if any(re.match(r"^        if\s*:", line) for line in step_lines):
        failures.append(
            "Validate Compose configurations step must not define an if condition"
        )

    env_header = "        env:"
    run_header = "        run: |"
    env_positions = [
        index for index, line in enumerate(step_lines) if line == env_header
    ]
    run_positions = [
        index for index, line in enumerate(step_lines) if line == run_header
    ]
    if len(env_positions) != 1 or len(run_positions) != 1:
        failures.append(
            "Validate Compose configurations step must contain one exact env "
            "block followed by one exact literal run block"
        )
        return
    env_position = env_positions[0]
    run_position = run_positions[0]
    next_env_sibling = next(
        (
            index
            for index in range(env_position + 1, len(step_lines))
            if WORKFLOW_STEP_KEY.match(step_lines[index])
        ),
        len(step_lines),
    )
    if next_env_sibling != run_position:
        failures.append(
            "Validate Compose configurations env block must be immediately "
            "followed by its literal run block"
        )
        return
    run_end = next(
        (
            index
            for index in range(run_position + 1, len(step_lines))
            if WORKFLOW_STEP_KEY.match(step_lines[index])
        ),
        len(step_lines),
    )
    env_lines = step_lines[env_position + 1 : run_position]
    run_lines = step_lines[run_position + 1 : run_end]
    env_controls = (
        (
            "fixed 9001 development frontend port",
            '          NEXPOLY_DEV_FRONTEND_PORT: "9001"',
        ),
        (
            "fixed development GPU session identity",
            '          NEXPOLY_DEV_GPU_SESSION_ID: "dddddddddddddddddddddddddddddddd"',
        ),
        (
            "isolated development GPU state root",
            "          NEXPOLY_GPU_STATE_ROOT: /tmp/nexpoly-gpu-state",
        ),
    )
    run_controls = (
        (
            "9001-only GPU-launcher Compose render",
            "          docker compose -f docker-compose.yml -f docker-compose.dev.yml "
            "-f docker-compose.dev-gpu-launcher.yml config --quiet",
        ),
        (
            "base, development, and GPU-session Compose render",
            "          docker compose -f docker-compose.yml -f docker-compose.dev.yml "
            "-f docker-compose.dev-gpu-session.yml config --quiet",
        ),
    )
    for label, line in env_controls:
        if env_lines.count(line) != 1:
            failures.append(
                f"Validate Compose configurations env must contain exactly one "
                f"active {label} control"
            )
    for label, line in run_controls:
        if run_lines.count(line) != 1:
            failures.append(
                f"Validate Compose configurations run block must contain exactly "
                f"one active {label} control"
            )
    require_ordered_markers(
        "\n".join(env_lines),
        env_controls,
        label="development GPU-session Compose environment",
        failures=failures,
    )
    require_ordered_markers(
        "\n".join(run_lines),
        run_controls,
        label="development GPU-session Compose render",
        failures=failures,
    )


def _unique_shell_section(
    text: str,
    *,
    start: str,
    end: str,
    label: str,
    failures: list[str],
) -> str | None:
    if text.count(start) != 1 or text.count(end) != 1:
        failures.append(f"{label} must have one exact start and end boundary")
        return None
    start_index = text.index(start)
    end_index = text.find(end, start_index + len(start))
    if end_index < 0:
        failures.append(f"{label} boundaries are out of order")
        return None
    return text[start_index:end_index]


def validate_exact_b_transition(text: str, failures: list[str]) -> None:
    digest_function = _unique_shell_section(
        text,
        start="pre_dft_mutable_digest() {",
        end="business_digest() {",
        label="exact-B pre-0014 mutable digest",
        failures=failures,
    )
    if digest_function is not None:
        expected_key_counts = {
            "'online_jobs'": 1,
            "'online_history'": 2,
            "'md_jobs'": 1,
            "'lab_test_projects'": 2,
            "'lab_sample_measurements'": 2,
            "'mutable_sequences'": 1,
        }
        for key, expected_count in expected_key_counts.items():
            if digest_function.count(key) != expected_count:
                failures.append(
                    "exact-B pre-0014 mutable digest has an incomplete or "
                    f"duplicated governed key: {key}"
                )
        require_ordered_markers(
            digest_function,
            (
                ("repeatable read-only snapshot", "REPEATABLE READ READ ONLY DEFERRABLE"),
                ("online jobs", "'online_jobs'"),
                ("online history", "'online_history'"),
                ("MD jobs", "'md_jobs'"),
                ("lab projects", "'lab_test_projects'"),
                ("lab measurements", "'lab_sample_measurements'"),
                ("sequence envelope", "'mutable_sequences'"),
                ("online history sequence", "'online_history'"),
                ("lab project sequence", "'lab_test_projects'"),
                ("lab measurement sequence", "'lab_sample_measurements'"),
                ("snapshot commit", "COMMIT;"),
            ),
            label="exact-B pre-0014 mutable digest",
            failures=failures,
        )

    transition = _unique_shell_section(
        text,
        start='b_transition_before="$(pre_dft_mutable_digest "$B_DATABASE")"',
        end=(
            'run_backend_command "$F_BACKEND_IMAGE" "$F_DATABASE" \\\n'
            "  python -m app.postgres_migrations --mode bootstrap"
        ),
        label=(
            "exact-B B/post-0012 through F/0013 compatibility and "
            "F/0014/F/0015 authority transition"
        ),
        failures=failures,
    )
    if transition is None:
        return
    mutable_unchanged = (
        '[[ "$(pre_dft_mutable_digest "$B_DATABASE")" == '
        '"$b_transition_before" ]]'
    )
    require_ordered_markers(
        transition,
        (
            (
                "sealed B/post-0012 mutable baseline",
                'b_transition_before="$(pre_dft_mutable_digest "$B_DATABASE")"',
            ),
            (
                "F applies the exact 0013 prefix to B",
                "run_backend_command_with_migrations \\\n"
                '  "$F_BACKEND_IMAGE" "$B_DATABASE" '
                '"$F_0013_MIGRATIONS_DIR" \\\n'
                "  python -m app.postgres_migrations --mode expand \\\n"
                "    --migrations-dir /tmp/nexpoly-migrations",
            ),
            (
                "exact intermediate 0013 ledger",
                "0013_monomer_dft_jobs:"
                "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc",
            ),
            ("post-0013 mutable digest", mutable_unchanged),
            (
                "F starts on the intermediate 0013 database",
                'start_backend "$F_BACKEND_IMAGE" "$B_DATABASE" '
                '"$b_transition_f_name" 18106',
            ),
            ("F reports the 0013 schema ready", "assert_dft_state 18106 true"),
            ("post-0013 F mutable digest", mutable_unchanged),
            (
                "B accepts the forward 0013 schema",
                'run_backend_command "$B_BACKEND_IMAGE" "$B_DATABASE" \\\n'
                "  python -m app.postgres_preflight --mode schema --strict",
            ),
            (
                "B starts on the forward 0013 schema",
                'start_backend "$B_BACKEND_IMAGE" "$B_DATABASE" '
                '"$b_transition_b_name" 18107',
            ),
            ("post-0013 B mutable digest", mutable_unchanged),
            (
                "F restarts on the 0013 database after B",
                "start_backend \\\n"
                '  "$F_BACKEND_IMAGE" "$B_DATABASE" '
                '"$b_transition_return_name" 18109',
            ),
            ("returned F reports the 0013 schema ready", "assert_dft_state 18109 true"),
            ("post-return-to-F 0013 mutable digest", mutable_unchanged),
            (
                "F applies the remaining canonical 0014/0015 migrations",
                'run_backend_command "$F_BACKEND_IMAGE" "$B_DATABASE" \\\n'
                "  python -m app.postgres_migrations --mode expand",
            ),
            (
                "exact final 0015 ledger",
                "0015_property_filter_performance:"
                "e0159576c09d31de8a7da46f728d36553f67aa75adba344f93cdc302cf000732",
            ),
            ("post-0015 mutable digest", mutable_unchanged),
            (
                "F starts on the final 0015 database",
                'start_backend "$F_BACKEND_IMAGE" "$B_DATABASE" '
                '"$b_transition_final_name" 18108',
            ),
            ("F reports the 0015 schema ready", "assert_dft_state 18108 true"),
            ("post-0015 F mutable digest", mutable_unchanged),
            (
                "B preflight is required to reject 0015",
                'if run_backend_command "$B_BACKEND_IMAGE" "$B_DATABASE" \\\n'
                "  python -m app.postgres_preflight --mode schema --strict",
            ),
            (
                "B rejection is explicit",
                "Exact B unexpectedly accepted the canonical 0015 ledger",
            ),
            ("post-rejected-B mutable digest", mutable_unchanged),
            (
                "F accepts the database after rejected B preflight",
                'run_backend_command "$F_BACKEND_IMAGE" "$B_DATABASE" \\\n'
                "  python -m app.postgres_preflight --mode schema --strict",
            ),
            ("final post-return-to-F mutable digest", mutable_unchanged),
        ),
        label=(
            "exact-B B/post-0012 through F/0013 compatibility and "
            "F/0014/F/0015 authority transition"
        ),
        failures=failures,
    )


def validate_exact_b_job(ci_text: str, failures: list[str]) -> None:
    body = workflow_job_body(ci_text, "exact-b-bridge", failures)
    if body is None:
        return
    require_ordered_markers(
        body,
        (
            ("immutable checkout assertion", "Assert immutable checkout"),
            ("PostgreSQL 16 client", "Install PostgreSQL 16 client"),
            ("private B image login", "Log in for the exact private B images"),
            (
                "real transition step",
                "Run real B-schema through F/0013, F/0014 and F/0015 transition smoke",
            ),
            ("exact transition script", "run: scripts/ci/test_exact_b_bridge.sh"),
        ),
        label="exact-b-bridge job",
        failures=failures,
    )


def validate_postgres_client_bootstrap(
    ci_text: str,
    bootstrap_payload: bytes,
    failures: list[str],
) -> None:
    helper = "scripts/ci/ensure_postgresql_16_client.sh"
    jobs = (
        (
            "production-alias-integration",
            "Install PostgreSQL 16 client",
            (
                "      - name: Install PostgreSQL 16 client",
                "        timeout-minutes: 9",
                "        run: |",
                f"          {helper}",
                (
                    '          "$NEXPOLY_ALIAS_TEST_PG_BIN/psql" --version '
                    '| grep -F "PostgreSQL) 16."'
                ),
                (
                    '          "$NEXPOLY_ALIAS_TEST_PG_BIN/pg_dump" --version '
                    '| grep -F "PostgreSQL) 16."'
                ),
                (
                    '          "$NEXPOLY_ALIAS_TEST_PG_BIN/pg_restore" --version '
                    '| grep -F "PostgreSQL) 16."'
                ),
            ),
        ),
        (
            "postgres-media-integration",
            "Install PostgreSQL 16 client for mutable-data audit",
            (
                (
                    "      - name: Install PostgreSQL 16 client for "
                    "mutable-data audit"
                ),
                "        if: matrix.major == '16'",
                "        timeout-minutes: 9",
                "        run: |",
                f"          {helper}",
                (
                    '          /usr/bin/psql --version | grep -F '
                    '"PostgreSQL) 16."'
                ),
            ),
        ),
        (
            "exact-b-bridge",
            "Install PostgreSQL 16 client",
            (
                "      - name: Install PostgreSQL 16 client",
                "        timeout-minutes: 9",
                "        run: |",
                f"          {helper}",
                '          psql --version | grep -F "PostgreSQL) 16."',
            ),
        ),
    )
    for job_name, step_name, expected_lines in jobs:
        body = workflow_job_body(ci_text, job_name, failures)
        if body is None:
            continue
        matching_steps = [
            step
            for step in workflow_step_blocks(body)
            if step.splitlines()
            and step.splitlines()[0] == f"      - name: {step_name}"
        ]
        if len(matching_steps) != 1:
            failures.append(
                f"{job_name} must contain one governed PostgreSQL 16 "
                "client bootstrap step"
            )
            continue
        actual_lines = tuple(matching_steps[0].rstrip().splitlines())
        if actual_lines != expected_lines:
            failures.append(
                f"{job_name} must retain the exact active PostgreSQL 16 "
                "client bootstrap step"
            )
    if ci_text.count(helper) != len(jobs):
        failures.append(
            "ci.yml must invoke the governed PostgreSQL 16 client bootstrap "
            "in exactly the three database integration jobs"
        )
    actual_bootstrap_sha256 = hashlib.sha256(bootstrap_payload).hexdigest()
    if actual_bootstrap_sha256 != EXPECTED_POSTGRES_CLIENT_BOOTSTRAP_SHA256:
        failures.append(
            "PostgreSQL 16 client bootstrap must match the exact reviewed "
            "fast-path and bounded-install implementation"
        )


def validate_release_input(failures: list[str]) -> None:
    try:
        document = json.loads(RELEASE_INPUT_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        failures.append(f"release-input.json is missing or invalid: {exc}")
        return
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "asset_manifest_digest",
        "predecessor_asset_manifest_digest",
        "changed_asset_trees",
        "datasets_on_asset_change",
    }:
        failures.append("release-input.json must contain exactly the reviewed schema fields")
        return
    if document["schema_version"] != 2:
        failures.append("release-input.json schema_version must be 2")
    digest = document["asset_manifest_digest"]
    if digest != EXPECTED_ASSET_DIGEST:
        failures.append("release-input.json asset_manifest_digest must equal frozen schema-v2")
    predecessor = document["predecessor_asset_manifest_digest"]
    if predecessor != EXPECTED_PREDECESSOR_ASSET_DIGEST:
        failures.append(
            "release-input.json predecessor_asset_manifest_digest must equal frozen schema-v1"
        )
    if document["changed_asset_trees"] != ["byteff2"]:
        failures.append("release-input.json must declare byteff2 as the only changed tree")
    datasets = document["datasets_on_asset_change"]
    if datasets != []:
        failures.append(
            "release-input.json must not rebuild PostgreSQL datasets for a ByteFF2-only asset change"
        )
    if not isinstance(datasets, list):
        return
    if len(datasets) != len(set(datasets)):
        failures.append("release-input.json datasets must be unique")
    if any(
        not isinstance(dataset, str)
        or not SAFE_DATASET.fullmatch(dataset)
        or dataset in {"all", "none"}
        for dataset in datasets
    ):
        failures.append("release-input.json contains an unsafe or implicit dataset name")


def validate_deployment_asset_pin(failures: list[str]) -> None:
    try:
        text = DEPLOYMENT_DOC_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        failures.append(f"deployment asset documentation is unavailable: {exc}")
        return
    matches = FROZEN_ASSET_DOC.findall(text)
    if matches != [EXPECTED_ASSET_DIGEST]:
        failures.append(
            "docs/deployment.md frozen schema-v2 digest must exactly match "
            "the reviewed release input"
        )


def _parse_contract_source(
    source_text: str,
    label: str,
    failures: list[str],
) -> ast.Module | None:
    try:
        return ast.parse(source_text, filename=label)
    except (SyntaxError, ValueError) as exc:
        failures.append(f"{label} contract source cannot be parsed: {exc}")
        return None


def _literal_module_assignments(module: ast.Module) -> dict[str, object]:
    assignments: dict[str, object] = {}
    for statement in module.body:
        name: str | None = None
        value: ast.expr | None = None
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            name = statement.targets[0].id
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(
            statement.target, ast.Name
        ):
            name = statement.target.id
            value = statement.value
        if name is None or value is None:
            continue
        try:
            assignments[name] = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            continue
    return assignments


def _module_assignment_expressions(module: ast.Module) -> dict[str, ast.expr]:
    expressions: dict[str, ast.expr] = {}
    for statement in module.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            expressions[statement.targets[0].id] = statement.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.value is not None
        ):
            expressions[statement.target.id] = statement.value
    return expressions


def _safe_contract_assignment(
    module: ast.Module,
    name: str,
) -> object | None:
    """Evaluate only literals, tuple concatenation, and record[0] tuples."""

    expressions = _module_assignment_expressions(module)
    cache: dict[str, object] = {}
    resolving: set[str] = set()

    def evaluate(expression: ast.expr) -> object:
        try:
            return ast.literal_eval(expression)
        except (ValueError, TypeError, SyntaxError):
            pass
        if isinstance(expression, ast.Name):
            reference = expression.id
            if reference in cache:
                return cache[reference]
            if reference in resolving or reference not in expressions:
                raise ValueError("unsafe or cyclic contract assignment")
            resolving.add(reference)
            try:
                value = evaluate(expressions[reference])
            finally:
                resolving.remove(reference)
            cache[reference] = value
            return value
        if isinstance(expression, ast.BinOp) and isinstance(
            expression.op, ast.Add
        ):
            left = evaluate(expression.left)
            right = evaluate(expression.right)
            if not isinstance(left, tuple) or not isinstance(right, tuple):
                raise ValueError("contract concatenation is not a tuple")
            return left + right
        if (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Name)
            and expression.func.id == "tuple"
            and len(expression.args) == 1
            and not expression.keywords
            and isinstance(expression.args[0], ast.GeneratorExp)
        ):
            generator = expression.args[0]
            if (
                len(generator.generators) != 1
                or generator.generators[0].is_async
                or generator.generators[0].ifs
                or not isinstance(generator.generators[0].target, ast.Name)
                or not isinstance(generator.elt, ast.Subscript)
                or not isinstance(generator.elt.value, ast.Name)
                or generator.elt.value.id
                != generator.generators[0].target.id
                or not isinstance(generator.elt.slice, ast.Constant)
                or generator.elt.slice.value != 0
            ):
                raise ValueError("contract generator is not record[0]")
            records = evaluate(generator.generators[0].iter)
            if not isinstance(records, tuple):
                raise ValueError("contract records are not a tuple")
            result: list[str] = []
            for record in records:
                if (
                    not isinstance(record, tuple)
                    or not record
                    or not isinstance(record[0], str)
                ):
                    raise ValueError("contract record path is invalid")
                result.append(record[0])
            return tuple(result)
        raise ValueError("contract assignment uses an unsafe expression")

    if name not in expressions:
        return None
    try:
        return evaluate(expressions[name])
    except ValueError:
        return None


def _named_function(
    module: ast.Module,
    name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == name
        ):
            return node
    return None


def _parser_choices(
    module: ast.Module,
    function_name: str,
    argument_name: str,
) -> tuple[str, ...] | None:
    function = _named_function(module, function_name)
    if function is None:
        return None
    for node in ast.walk(function):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr != "add_argument"
            or not node.args
            or not isinstance(node.args[0], ast.Constant)
            or node.args[0].value != argument_name
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg != "choices":
                continue
            try:
                choices = ast.literal_eval(keyword.value)
            except (ValueError, TypeError, SyntaxError):
                return None
            if (
                isinstance(choices, tuple)
                and all(isinstance(choice, str) for choice in choices)
            ):
                return choices
    return None


def _function_string_literals(
    module: ast.Module,
    function_name: str,
) -> set[str]:
    function = _named_function(module, function_name)
    if function is None:
        return set()
    return {
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _parser_has_literal_loop(
    module: ast.Module,
    function_name: str,
    expected: tuple[str, ...],
) -> bool:
    function = _named_function(module, function_name)
    if function is None:
        return False
    for node in ast.walk(function):
        if not isinstance(node, ast.For):
            continue
        try:
            value = ast.literal_eval(node.iter)
        except (ValueError, TypeError, SyntaxError):
            continue
        if value == expected:
            return True
    return False


def _is_named_subscript(
    node: ast.AST,
    container: str,
    key: str,
) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == container
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == key
    )


def _unit_schema_contract_is_v2(module: ast.Module) -> bool:
    plan_function = _named_function(module, "_unit_source_plan")
    authority_function = _named_function(module, "_unit_authority")
    if plan_function is None or authority_function is None:
        return False
    conditional_schema = False
    for node in ast.walk(plan_function):
        if (
            not isinstance(node, ast.Assign)
            or len(node.targets) != 1
            or not isinstance(node.targets[0], ast.Name)
            or node.targets[0].id != "schema_version"
            or not isinstance(node.value, ast.IfExp)
        ):
            continue
        test = node.value.test
        conditional_schema = (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Constant)
            and test.left.value
            == "adopted_git_permission_source_successor_sha256"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.In)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Name)
            and test.comparators[0].id == "context"
            and isinstance(node.value.body, ast.Constant)
            and node.value.body.value == 2
            and isinstance(node.value.orelse, ast.Constant)
            and node.value.orelse.value == 1
        )
        if conditional_schema:
            break
    authority_propagates_plan_schema = False
    for node in ast.walk(authority_function):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "schema_version"
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "int"
            and len(node.value.args) == 1
            and _is_named_subscript(
                node.value.args[0], "plan", "schema_version"
            )
        ):
            authority_propagates_plan_schema = True
            break
    return conditional_schema and authority_propagates_plan_schema


def _controller_requires_v2_unit_lineage(module: ast.Module) -> bool:
    function = _named_function(
        module,
        "_current_adoption_successor_lineage",
    )
    if function is None:
        return False
    for node in ast.walk(function):
        if (
            not isinstance(node, ast.Compare)
            or len(node.ops) != 1
            or not isinstance(node.ops[0], ast.NotEq)
            or len(node.comparators) != 1
            or not isinstance(node.comparators[0], ast.Constant)
            or node.comparators[0].value != 2
            or not isinstance(node.left, ast.Call)
            or not isinstance(node.left.func, ast.Attribute)
            or node.left.func.attr != "get"
            or not isinstance(node.left.func.value, ast.Name)
            or node.left.func.value.id != "unit"
            or len(node.left.args) != 1
            or not isinstance(node.left.args[0], ast.Constant)
            or node.left.args[0].value != "schema_version"
        ):
            continue
        return True
    return False


def validate_successor_authority_contract_source_text(
    source_successor_text: str,
    prerequisites_text: str,
    controller_text: str,
    failures: list[str],
) -> None:
    """Cross-check fixed successor constants without importing live tools."""

    modules = (
        _parse_contract_source(
            source_successor_text,
            "scripts/adopt_git_permission_source_successor.py",
            failures,
        ),
        _parse_contract_source(
            prerequisites_text,
            "scripts/adopt_runtime_prerequisites.py",
            failures,
        ),
        _parse_contract_source(
            controller_text,
            "scripts/pull_deploy_controller.py",
            failures,
        ),
    )
    if any(module is None for module in modules):
        return
    successor_module, prerequisites_module, controller_module = modules
    assert successor_module is not None
    assert prerequisites_module is not None
    assert controller_module is not None
    successor = _literal_module_assignments(successor_module)
    prerequisites = _literal_module_assignments(prerequisites_module)
    controller = _literal_module_assignments(controller_module)

    derived_manifests = (
        successor.get("TRACKED_SOURCE_FILES"),
        _safe_contract_assignment(
            prerequisites_module,
            "UNIT_PERMISSION_SUCCESSOR_V2_BLOBS",
        ),
        _safe_contract_assignment(
            controller_module,
            "ADOPTED_UNIT_PERMISSION_SUCCESSOR_V2_FILES",
        ),
    )
    if any(
        manifest != EXPECTED_SOURCE_SUCCESSOR_MANIFEST
        for manifest in derived_manifests
    ):
        failures.append(
            "source-successor publisher, adopter, and controller must bind "
            "the same fixed 13-file manifest"
        )
    changed_path_sets = (
        successor.get("CHANGED_PATHS"),
        prerequisites.get("SOURCE_SUCCESSOR_ALLOWED_CHANGED_BLOBS"),
        controller.get(
            "ADOPTED_GIT_PERMISSION_SOURCE_SUCCESSOR_ALLOWED_CHANGED_FILES"
        ),
    )
    if any(
        changed != EXPECTED_SOURCE_SUCCESSOR_CHANGED_PATHS
        for changed in changed_path_sets
    ):
        failures.append(
            "source-successor implementations must authorize exactly the "
            "two fixed changed paths"
        )

    transition_contracts = (
        (
            successor.get("REPOSITORY_TRANSITION_POLICY"),
            successor.get("DEPLOY_REMOTE_REF"),
            successor.get("PREPARED_REF_PREFIX"),
            successor.get("GIT_AUXILIARY_POLICY"),
            successor.get("GIT_OBJECT_STORAGE_POLICY"),
        ),
        (
            prerequisites.get(
                "SOURCE_SUCCESSOR_REPOSITORY_TRANSITION_POLICY"
            ),
            prerequisites.get("SOURCE_SUCCESSOR_DEPLOY_REMOTE_REF"),
            prerequisites.get("SOURCE_SUCCESSOR_PREPARED_REF_PREFIX"),
            prerequisites.get("SOURCE_SUCCESSOR_GIT_AUXILIARY_POLICY"),
            prerequisites.get(
                "SOURCE_SUCCESSOR_GIT_OBJECT_STORAGE_POLICY"
            ),
        ),
        (
            controller.get("PRODUCTION_REPOSITORY_TRANSITION_POLICY"),
            controller.get("DEPLOY_REMOTE_REF"),
            controller.get("PREPARED_REF_PREFIX"),
            controller.get("GIT_AUXILIARY_POLICY"),
            controller.get("GIT_OBJECT_STORAGE_POLICY"),
        ),
    )
    if len(set(transition_contracts)) != 1:
        failures.append(
            "source-successor publisher, adopter, and controller must bind "
            "the same B/F/P repository, auxiliary, and object-storage policy"
        )

    provenance = successor.get("EXPECTED_PREDECESSOR_PROVENANCE")
    predecessor_trust = (
        provenance.get("predecessor_source_trust_sha256")
        if isinstance(provenance, dict)
        else None
    )
    production_trust = (
        provenance.get("production_source_trust_sha256")
        if isinstance(provenance, dict)
        else None
    )
    if (
        not isinstance(predecessor_trust, str)
        or DIGEST.fullmatch(predecessor_trust) is None
        or not isinstance(production_trust, str)
        or DIGEST.fullmatch(production_trust) is None
        or predecessor_trust == production_trust
    ):
        failures.append(
            "source-successor predecessor/current source trust must remain "
            "two distinct digest proofs"
        )

    if _parser_choices(
        successor_module,
        "_parser",
        "action",
    ) != ("plan", "apply", "abort") or not {
        "--confirm-plan-sha256",
        "--confirm-source-successor-impact-sha256",
    }.issubset(_function_string_literals(successor_module, "_parser")):
        failures.append(
            "source-successor parser must expose exact plan/apply/abort "
            "commands and both confirmations"
        )
    if not _parser_has_literal_loop(
        prerequisites_module,
        "_parser",
        (
            "unit-permission-plan",
            "unit-permission-apply",
            "unit-permission-abort",
        ),
    ) or not {
        "--confirm-plan-sha256",
        "--confirm-unit-permission-impact-sha256",
    }.issubset(_function_string_literals(prerequisites_module, "_parser")):
        failures.append(
            "unit successor parser must expose exact plan/apply/abort "
            "commands and both confirmations"
        )
    if not _parser_has_literal_loop(
        controller_module,
        "parser",
        ("plan", "prepare", "apply", "accept"),
    ):
        failures.append(
            "Pull controller parser must retain exact ordinary deployment "
            "commands"
        )

    if not _unit_schema_contract_is_v2(prerequisites_module):
        failures.append(
            "source-successor unit plan and final authority must use schema v2"
        )
    if (
        controller.get("DESCRIPTOR_SCHEMA_VERSION") != 4
        or controller.get("CURRENT_STATE_SCHEMA_VERSION") != 3
    ):
        failures.append(
            "ordinary successor deployment must use descriptor v4 and "
            "current-state v3"
        )
    if (
        controller.get("ADOPTION_SUCCESSOR_LINEAGE_FIELDS")
        != EXPECTED_ADOPTION_SUCCESSOR_LINEAGE_FIELDS
    ):
        failures.append(
            "current-state successor lineage must contain exactly its eight "
            "permanent raw-authority anchors"
        )
    if not _controller_requires_v2_unit_lineage(controller_module):
        failures.append(
            "current-state successor lineage must reject a non-v2 unit authority"
        )


def validate_successor_authority_contract_sources(
    failures: list[str],
) -> None:
    try:
        source_successor_text = SOURCE_SUCCESSOR_PATH.read_text(
            encoding="utf-8"
        )
        prerequisites_text = ADOPT_RUNTIME_PREREQUISITES_PATH.read_text(
            encoding="utf-8"
        )
        controller_text = PULL_DEPLOY_CONTROLLER_PATH.read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError) as exc:
        failures.append(f"successor authority contract source is unavailable: {exc}")
        return
    validate_successor_authority_contract_source_text(
        source_successor_text,
        prerequisites_text,
        controller_text,
        failures,
    )


def _validate_ordered_ordinary_commands(
    text: str,
    *,
    label: str,
    start_marker: str,
    end_marker: str,
    failures: list[str],
) -> None:
    start = text.find(start_marker)
    end = text.find(end_marker, start + len(start_marker))
    if start < 0 or end < 0:
        failures.append(
            f"{label} must isolate the first ordinary deployment sequence"
        )
        return
    section = text[start:end]
    positions = [section.find(command) for command in ORDINARY_DEPLOYMENT_COMMANDS]
    if (
        any(section.count(command) != 1 for command in ORDINARY_DEPLOYMENT_COMMANDS)
        or positions != sorted(positions)
        or any(position < 0 for position in positions)
    ):
        failures.append(
            f"{label} must order one exact direct plan, installed prepare, "
            "rehearsal plan, rehearsal apply, and installed apply sequence"
        )


def _validate_ordered_successor_first_deployment_commands(
    text: str,
    *,
    label: str,
    start_marker: str,
    end_marker: str,
    commands: tuple[str, ...],
    failures: list[str],
) -> None:
    start = text.find(start_marker)
    end = text.find(end_marker, start + len(start_marker))
    if start < 0 or end < 0:
        failures.append(
            f"{label} must isolate the successor first-deployment sequence"
        )
        return
    section = text[start:end]
    positions = [section.find(command) for command in commands]
    if (
        any(section.count(command) != 1 for command in commands)
        or positions != sorted(positions)
        or any(position < 0 for position in positions)
    ):
        failures.append(
            f"{label} must order one exact snapshot, source-successor, "
            "unit-permission, bootstrap-router, mutable-data role, Pull, "
            "rehearsal, and apply sequence"
        )


def validate_adopted_permission_documentation_text(
    deployment_text: str,
    controller_text: str,
    failures: list[str],
) -> None:
    deployment_start = deployment_text.find(
        "### One-time adopted Git permission hardening"
    )
    deployment_end = deployment_text.find(
        "Next provision the dedicated mutable-data audit login.",
        deployment_start,
    )
    if deployment_start < 0 or deployment_end < 0:
        failures.append(
            "docs/deployment.md must isolate the current adopted Git "
            "permission workflow before role provisioning"
        )
        return
    deployment_section = deployment_text[deployment_start:deployment_end]
    controller_start = controller_text.find(
        "## Current production authority and prerequisites"
    )
    controller_end = controller_text.find("## Commands", controller_start)
    if controller_start < 0 or controller_end < 0:
        failures.append(
            "docs/release-controller.md must isolate current production "
            "permission authority before controller commands"
        )
        return
    controller_section = controller_text[controller_start:controller_end]

    for label, document in (
        ("docs/deployment.md", deployment_text),
        ("docs/release-controller.md", controller_text),
    ):
        invalid = [
            marker
            for marker in CONTROL_CHAIN_ONLY_COMMAND_MARKERS
            if document.count(marker) != 1
        ]
        if invalid:
            failures.append(
                f"{label} must mark all mutating command examples as "
                "future-state only: " + ", ".join(invalid)
            )

    exact_commands = (
        "./scripts/adopt_runtime_prerequisites.py permission-plan \\\n"
        "  --sha <full-main-sha> \\\n"
        "  --operation-id \"$permission_operation_id\"",
        "./scripts/adopt_runtime_prerequisites.py permission-apply \\\n"
        "  --sha <full-main-sha> \\\n"
        "  --operation-id \"$permission_operation_id\" \\\n"
        "  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \\\n"
        "  --confirm-permission-impact-sha256 "
        "sha256:<reviewed-impact-digest>",
        "./scripts/adopt_runtime_prerequisites.py permission-abort \\\n"
        "  --sha <full-main-sha> \\\n"
        "  --operation-id \"$permission_operation_id\" \\\n"
        "  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \\\n"
        "  --confirm-permission-impact-sha256 "
        "sha256:<reviewed-impact-digest>",
    )
    source_successor_commands = (
        SOURCE_SUCCESSOR_PLAN_COMMAND,
        SOURCE_SUCCESSOR_APPLY_COMMAND,
        "./scripts/adopt_git_permission_source_successor.py abort \\\n"
        "  --sha <full-main-sha> \\\n"
        "  --operation-id \"$source_successor_operation_id\" \\\n"
        "  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \\\n"
        "  --confirm-source-successor-impact-sha256 "
        "sha256:<reviewed-impact-digest>",
    )
    unit_permission_commands = (
        UNIT_PERMISSION_PLAN_COMMAND,
        UNIT_PERMISSION_APPLY_COMMAND,
        UNIT_PERMISSION_ABORT_COMMAND,
    )
    snapshot_commands = (
        PRODUCTION_GIT_SNAPSHOT_PLAN_COMMAND,
        PRODUCTION_GIT_SNAPSHOT_APPLY_COMMAND,
    )
    router_commands = (
        BOOTSTRAP_ROUTER_PLAN_COMMAND,
        BOOTSTRAP_ROUTER_APPLY_COMMAND,
    )
    for label, section in (
        ("docs/deployment.md", deployment_section),
        ("docs/release-controller.md", controller_section),
    ):
        normalized_section = " ".join(section.split())
        required_unit_commands = (
            unit_permission_commands
            if label == "docs/deployment.md"
            else unit_permission_commands[:2]
        )
        if (
            section.count(
                "permission_operation_id="
                "adopt-git-permission-<utc-timestamp>"
            )
            != 1
            or any(section.count(command) != 1 for command in exact_commands)
            or section.count(
                "snapshot_operation_id=snapshot-git-<utc-timestamp>"
            )
            != 1
            or any(
                section.count(command) != 1
                for command in snapshot_commands
            )
            or section.count(
                "source_successor_operation_id="
                "adopt-git-successor-<utc-timestamp>"
            )
            != 1
            or any(
                section.count(command) != 1
                for command in source_successor_commands
            )
            or any(
                section.count(command) != 1
                for command in required_unit_commands
            )
            or section.count(
                "router_operation_id=adopt-router-<utc-timestamp>"
            )
            != 1
            or any(
                section.count(command) != 1
                for command in router_commands
            )
        ):
            failures.append(
                f"{label} must contain one exact permission, snapshot, "
                "source-successor, unit-permission, and bootstrap-router "
                "plan/apply sequence, with the deployment runbook also "
                "carrying exact abort"
            )
        # The following Worker-unit transaction deliberately uses the same
        # generic plan-confirmation flag.  Count only the Git-permission-
        # specific impact confirmations here; ``exact_commands`` above
        # already proves that both Git apply and Git abort also carry their
        # plan confirmation.
        if section.count("--confirm-permission-impact-sha256") != 2:
            failures.append(
                f"{label} permission apply and abort must both require the "
                "plan and impact confirmations"
            )
        if (
            section.count(
                "--confirm-source-successor-impact-sha256"
            )
            != 2
        ):
            failures.append(
                f"{label} source-successor apply and abort must both require "
                "the plan and impact confirmations"
            )
        required = (
            "state/adopted-git-permissions.json",
            "manual-runtime-adoption-permission-hardening",
            "state/adopted-git-permission-transactions/<operation-id>.json",
            "state/deploy.lock",
            "`intent`",
            "permission-change-intent",
            "permission-ready",
            "source-verified",
            "authority-commit-intent",
            "`completed`",
            "aborted",
            "forward-only",
            "167",
            "not a policy constant",
            "checkout root and `.git/**`",
            "ordinary working-tree files",
            "lowercase, colon-free",
            "does not invoke",
            "compact permission",
            "state/legacy-git-permission-takeover.json",
            "git_source_trust.takeover_repository_permissions",
            "install_legacy_takeover_prerequisites.py",
            "authority_publication",
            "initially_absent=true",
            ".adopted-unit-permissions.json.create-<operation-id>",
            "same-operation weak authority",
            "single-link final authority",
            "state/adopted-git-permission-source-successor.json",
            "state/adopted-git-permission-source-successor-transactions/"
            "<operation-id>.json",
            "standard-library-only",
            "bridge_deploy_core.py",
            "predecessor-verified",
            "source-successor impact",
            "old-root → source-successor",
            "state/production-git-snapshot.json",
            "state/bootstrap-router-successor-intent.json",
            "state/bootstrap-router-successor.json",
            "manual-runtime-adoption-bootstrap-router-successor",
            "active-control",
            "contract-0012-in-progress.json",
            "whole-directory",
            "fixed 13-file manifest",
            "`predecessor_source_trust_sha256`",
            "`production_source_trust_sha256`",
            "separate evidence and must not be compared for equality",
            "source-successor-bearing raw adoption",
            "`state/adopted-unit-permissions.json` authority must all use "
            "schema v2",
            "The resulting raw adoption must emit descriptor v4 and, after "
            "a successful deployment, current-state v3",
            "`adoption_successor_lineage`",
            "`source_successor_authority_sha256`",
            "`source_successor_completed_journal_sha256`",
            "`unit_permission_authority_sha256`",
            "`unit_permission_completed_journal_sha256`",
            "`unit_permission_transaction_inventory_sha256`",
            "`production_git_snapshot_authority_sha256`",
            "`bootstrap_router_intent_sha256`",
            "`bootstrap_router_authority_sha256`",
            "Historical current-state v3 records may omit",
            "once written, all eight anchors are permanent",
            "`B` is",
            "`F`",
            "`P(operation)`",
            "`FETCH_HEAD`",
            "`tmp_pack_*`",
            "semantic",
            "object-storage",
            "power loss",
            "restore the entire `.git` directory",
            *SOURCE_SUCCESSOR_MAIN_FREEZE_MARKERS,
        )
        if label == "docs/release-controller.md":
            required += ("`unit-permission-abort` and both confirmations",)
        missing = [
            marker for marker in required if marker not in normalized_section
        ]
        if missing:
            failures.append(
                f"{label} adopted Git permission policy is incomplete: "
                + ", ".join(missing)
            )
        if "./scripts/install_legacy_takeover_prerequisites.py \\" in section:
            failures.append(
                f"{label} current adopted workflow must not execute the "
                "legacy permission installer"
            )

    _validate_ordered_ordinary_commands(
        deployment_text,
        label="docs/deployment.md",
        start_marker="## Current ordinary deployments",
        end_marker="## Historical migrations and first takeover",
        failures=failures,
    )
    _validate_ordered_ordinary_commands(
        controller_text,
        label="docs/release-controller.md",
        start_marker=(
            "There is one compatibility exception before the first ordinary "
            "deployment"
        ),
        end_marker="## Runtime and slot records",
        failures=failures,
    )
    _validate_ordered_successor_first_deployment_commands(
        deployment_text,
        label="docs/deployment.md",
        start_marker="### One-time Git permission source-successor authority",
        end_marker="## Historical migrations and first takeover",
        commands=DEPLOYMENT_SUCCESSOR_FIRST_DEPLOYMENT_COMMANDS,
        failures=failures,
    )
    _validate_ordered_successor_first_deployment_commands(
        controller_text,
        label="docs/release-controller.md",
        start_marker="First create the independent whole-directory snapshot authority:",
        end_marker="## Runtime and slot records",
        commands=CONTROLLER_SUCCESSOR_FIRST_DEPLOYMENT_COMMANDS,
        failures=failures,
    )


def validate_adopted_permission_documentation(
    failures: list[str],
) -> None:
    try:
        deployment_text = DEPLOYMENT_DOC_PATH.read_text(encoding="utf-8")
        controller_text = RELEASE_CONTROLLER_DOC_PATH.read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError) as exc:
        failures.append(
            f"adopted Git permission documentation is unavailable: {exc}"
        )
        return
    validate_adopted_permission_documentation_text(
        deployment_text,
        controller_text,
        failures,
    )


def validate_exact_b_bridge(failures: list[str]) -> None:
    try:
        text = EXACT_B_BRIDGE_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        failures.append(f"exact-B bridge validation is unavailable: {exc}")
        return
    for marker in (
        f'readonly B_SHA="{EXPECTED_B_SHA}"',
        f'readonly B_TREE="{EXPECTED_B_TREE}"',
        f'readonly B_BRIDGE_CORE_BLOB="{EXPECTED_B_BRIDGE_CORE_BLOB}"',
        f'readonly B_BACKEND_IMAGE="{EXPECTED_B_BACKEND_IMAGE}"',
        f'readonly B_WEB_IMAGE="{EXPECTED_B_WEB_IMAGE}"',
        '"${B_SHA}:scripts/bridge_deploy_core.py")" == "$B_BRIDGE_CORE_BLOB"',
        'git merge-base --is-ancestor "$B_SHA" "$candidate_sha"',
        'python -m app.postgres_migrations --mode bootstrap',
        "schema_not_ready",
        'assert_schema_not_ready_route "$port" POST "/jobs" "$submit_body"',
        'assert_schema_not_ready_route "$port" DELETE "/jobs/${job_id}/artifacts"',
        "'lab_test_projects'",
        "'lab_sample_measurements'",
        "'mutable_sequences'",
        "assert_frozen_b_parser_accepts_policy",
        '"$REPOSITORY_ROOT/ops/config/production-bridge-policy.json"',
        "parsed_by_b = b.parse_policy(policy_bytes)",
        '"media_authority_rules_sha256"',
        '"audit_role_sql_sha256"',
        "--add-host backend:127.0.0.1",
        '[[ "$(business_digest "$F_DATABASE")" == "$before_digest" ]]',
    ):
        if marker not in text:
            failures.append(f"exact-B bridge validation is missing: {marker}")
    if "/data/lzq/gith/nexpoly" in text or "GPU 2" in text:
        failures.append(
            "exact-B bridge validation must not reference production paths or GPU 2"
        )
    if "media_registry_sha256" in text:
        failures.append(
            "exact-B bridge validation must use schema-bound authority digests"
        )
    validate_exact_b_transition(text, failures)


def main() -> int:
    failures: list[str] = []
    workflow_files = sorted((*WORKFLOW_ROOT.glob("*.yml"), *WORKFLOW_ROOT.glob("*.yaml")))
    names = {path.name for path in workflow_files}
    if names != {"ci.yml"}:
        failures.append(
            "the only active workflow must be ci.yml; found: "
            + (", ".join(sorted(names)) or "none")
        )
    if not CI_PATH.is_file():
        failures.append("ci.yml is missing")
        ci_text = ""
    else:
        ci_text = CI_PATH.read_text(encoding="utf-8")
    if (
        BACKEND_DOCKERFILE_PATH.is_symlink()
        or not BACKEND_DOCKERFILE_PATH.is_file()
    ):
        failures.append("Backend Dockerfile is missing or unsafe")
        backend_dockerfile_text = ""
    else:
        backend_dockerfile_text = BACKEND_DOCKERFILE_PATH.read_text(
            encoding="utf-8"
        )
    if (
        BACKEND_IMAGE_ASSERTION_PATH.is_symlink()
        or not BACKEND_IMAGE_ASSERTION_PATH.is_file()
    ):
        failures.append("Backend image identity assertion script is missing or unsafe")
        backend_image_assertion = b""
    else:
        backend_image_assertion = BACKEND_IMAGE_ASSERTION_PATH.read_bytes()
        assertion_mode = stat.S_IMODE(BACKEND_IMAGE_ASSERTION_PATH.stat().st_mode)
        if assertion_mode != 0o755:
            failures.append(
                "Backend image identity assertion script must use mode 0755"
            )
    if (
        POSTGRES_CLIENT_BOOTSTRAP_PATH.is_symlink()
        or not POSTGRES_CLIENT_BOOTSTRAP_PATH.is_file()
    ):
        failures.append(
            "PostgreSQL 16 client bootstrap script is missing or unsafe"
        )
        postgres_client_bootstrap = b""
    else:
        postgres_client_bootstrap = POSTGRES_CLIENT_BOOTSTRAP_PATH.read_bytes()
        bootstrap_mode = stat.S_IMODE(
            POSTGRES_CLIENT_BOOTSTRAP_PATH.stat().st_mode
        )
        if bootstrap_mode != 0o755:
            failures.append(
                "PostgreSQL 16 client bootstrap script must use mode 0755"
            )
    if LEGACY_REMOTE_RELEASE_PATH.exists() or LEGACY_REMOTE_RELEASE_PATH.is_symlink():
        failures.append("the legacy CI-to-production remote release transport must stay removed")

    for path in workflow_files:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ANY_ACTION.match(line) and not PINNED_ACTION.match(line):
                failures.append(
                    f"{path.relative_to(REPOSITORY_ROOT)}:{line_number}: "
                    "action is not pinned to a full commit SHA"
                )

    require_markers(
        ci_text,
        (
            "pull_request:",
            "push:",
            "branches: [main]",
            "[[ \"$EVENT_REF\" == refs/pull/*/merge ]]",
            "  bridge-validation:\n"
            "    name: bridge-validation\n"
            "    needs: resolve-sha\n"
            "    runs-on: ubuntu-24.04",
            "Validate exact bridge policy and schema compatibility states",
            "python3 scripts/validate_production_bridge_policy.py",
            "scripts/tests/test_production_bridge_policy.py",
            "  exact-b-bridge:\n"
            "    name: exact-B bridge compatibility\n"
            "    needs: resolve-sha\n"
            "    runs-on: ubuntu-24.04",
            "Run real B-schema through F/0013, F/0014 and F/0015 transition smoke",
            "scripts/ci/test_exact_b_bridge.sh",
            "name: ci-gate",
            "  release:\n"
            "    name: Publish and smoke immutable main images\n"
            "    if: >-\n"
            "      !cancelled() &&\n"
            "      needs.ci-gate.result == 'success' &&\n"
            "      github.event_name == 'push'\n"
            "    needs: [resolve-sha, ci-gate]",
            "python3 scripts/ci/backend_test_shards.py --shards 3 --shard",
            "name: Monomer-DFT Worker tests",
            "python-version: \"3.12\"",
            "workers/monomer_dft_worker/requirements-ci.lock",
            "env -u PYTHONPATH python -m pytest workers/monomer_dft_worker/tests",
            "scripts/tests/test_monomer_md_worker_launcher.py",
            "scripts/tests/test_worker_slot_runtime.py",
            "scripts/tests/test_dev_gpu_session.py",
            "scripts/tests/test_dev_worker_process.py",
            "Rebuild the production Worker runtime lock from empty",
            'python -m venv --clear "$runtime_venv"',
            '"$runtime_venv/bin/python" -m pip install',
            "--require-hashes --only-binary=:all:",
            "workers/monomer_md_worker/requirements.lock",
            '"$runtime_venv/bin/python" -m pip check',
            "Prepare the exact development Worker test runtime",
            "python scripts/prepare_dev_worker_venv.py prepare",
            'worker_python="$GITHUB_WORKSPACE/.venv-monomer-md-worker/bin/python"',
            '"$worker_python" -m pip install --require-hashes',
            "python scripts/prepare_dev_worker_venv.py verify",
            '"$worker_python" -m pytest -p no:cacheprovider',
            "working-directory: frontend",
            "run: npm test",
            "scripts/ci/test_frontend_image_permissions.sh",
            "python3 -m unittest -v \"${unittest_files[@]}\"",
            "name: Production 0005 alias PostgreSQL 16 isolation integration",
            'NEXPOLY_ALIAS_DOCKER_INTEGRATION: "1"',
            "NEXPOLY_ALIAS_DOCKER_TEST_ACK: ephemeral-localhost-only",
            "NEXPOLY_ALIAS_TEST_PG_BIN: /usr/lib/postgresql/16/bin",
            "  postgres-media-integration:\n"
            "    name: PostgreSQL media matching-major integration "
            "(${{ matrix.major }})\n"
            "    needs: resolve-sha\n"
            "    runs-on: ubuntu-24.04",
            'NEXPOLY_RUN_POSTGRES_MEDIA_INTEGRATION: "1"',
            'NEXPOLY_RUN_MUTABLE_HELPER_INTEGRATION: "1"',
            "NEXPOLY_POSTGRES_MEDIA_TEST_ACK: ephemeral-localhost-only",
            "NEXPOLY_TEST_POSTGRES_MAJOR: ${{ matrix.major }}",
            "NEXPOLY_TEST_POSTGRES_IMAGE: ${{ matrix.image }}",
            "Install PostgreSQL 16 client for mutable-data audit",
            "scripts/ci/ensure_postgresql_16_client.sh",
            '/usr/bin/psql --version | grep -F "PostgreSQL) 16."',
            *EXPECTED_POSTGRES_AUDIT_IMAGES,
            "docker pull \"$POSTGRES_IMAGE\"",
            "docker pull \"$NEXPOLY_TEST_POSTGRES_IMAGE\"",
            "test_reconcile_production_0005_polytao_alias_integration.py",
            "Run real matching-major external-media integration",
            (
                "scripts.tests.test_postgres_media_evidence."
                "RealDockerPostgresIntegrationTests"
            ),
            "      - production-alias-integration",
            "      - postgres-media-integration",
            "      - bridge-validation",
            "      - exact-b-bridge",
            "python3 scripts/ci/validate_dependency_locks.py",
            "python3 -m app.migration_policy",
            "python3 scripts/validate_monomer_dft_release_contract.py --require-committed",
            "docker-compose.monomer-dft-dev.yml config --quiet",
            "docker compose -f docker-compose.yml -f docker-compose.prod.yml config --quiet",
            "if: github.event_name == 'pull_request'",
            "push: false",
            "push: true",
            "ghcr.io/lzq390/nexpoly-backend:sha-",
            "ghcr.io/lzq390/nexpoly-web:sha-",
            "BACKEND_IMAGE=ghcr.io/lzq390/nexpoly-backend@${BACKEND_DIGEST}",
            "WEB_IMAGE=ghcr.io/lzq390/nexpoly-web@${WEB_DIGEST}",
            "python -m app.postgres_migrations --mode bootstrap",
            "python -m app.postgres_preflight --mode schema --strict",
            "asset_path=\"$(grep -Eo",
        ),
        failures,
    )
    validate_complete_history_checkouts(ci_text, failures)
    validate_script_tests_budget(ci_text, failures)
    validate_gpu_session_compose_policy(ci_text, failures)
    validate_exact_b_job(ci_text, failures)
    validate_postgres_client_bootstrap(
        ci_text,
        postgres_client_bootstrap,
        failures,
    )
    validate_backend_image_identity_policy(ci_text, failures)
    validate_openscience_release_policy(ci_text, failures)
    validate_backend_dockerfile_identity_policy(
        backend_dockerfile_text,
        failures,
    )
    validate_backend_image_assertion_script(
        backend_image_assertion,
        failures,
    )

    for forbidden in (
        "workflow_run:",
        "workflow_dispatch:",
        "merge_group:",
        "actions/upload-artifact",
        "actions/download-artifact",
        "nexpoly-control:sha-",
        "nexpoly-worker:sha-",
        "--control-archive",
        "--worker-archive",
        "ssh-keyscan",
        "release_sha:",
        "PR_MERGE_SHA",
        "dataset all",
        "AUTODEPLOY_ENABLED",
        "environment: nexpoly-production",
        "NEXPOLY_SSH_",
        "scripts/ci/remote_release.sh",
        "release-bundle",
        "nexpoly-release-${RELEASE_SHA}.tar.gz",
        "dist/release-manifest.json",
        "git archive",
        "python -m pip download",
        "provision-release",
        "deploy --apply",
    ):
        if forbidden in ci_text:
            failures.append(f"ci.yml contains forbidden legacy/implicit control: {forbidden}")

    if ci_text.count("push: true") != 3:
        failures.append(
            "ci.yml must push exactly the Backend, Web, and OpenScience UI SHA images"
        )
    if ci_text.count("ghcr.io/lzq390/nexpoly-backend:sha-") != 2:
        failures.append(
            "ci.yml must publish and finally seal one immutable Backend SHA tag"
        )
    if ci_text.count("ghcr.io/lzq390/nexpoly-web:sha-") != 2:
        failures.append(
            "ci.yml must publish and finally seal one immutable Web SHA tag"
        )
    if ci_text.count("ghcr.io/lzq390/openscience-ui:sha-") != 2:
        failures.append(
            "ci.yml must publish and finally seal one immutable OpenScience UI SHA tag"
        )
    if ci_text.count(
        "org.opencontainers.image.revision=${{ needs.resolve-sha.outputs.candidate_sha }}"
    ) != 3:
        failures.append("all published images must bind the immutable source revision label")
    if ci_text.count(
        "org.opencontainers.image.source=${{ github.server_url }}/${{ github.repository }}"
    ) != 3:
        failures.append("all published images must bind the repository source label")
    if ci_text.count(
        "org.opencontainers.image.version=sha-${{ needs.resolve-sha.outputs.candidate_sha }}"
    ) != 3:
        failures.append("all published images must bind the immutable SHA version label")
    if ci_text.count("uses: actions/checkout@") != ci_text.count("Assert immutable checkout"):
        failures.append("every checkout must be followed by an immutable SHA assertion")
    if ci_text.count("uses: actions/checkout@") != ci_text.count("persist-credentials: false"):
        failures.append("every checkout must disable persisted GitHub credentials")
    exact_candidate_ref = (
        "ref: ${{ needs.resolve-sha.outputs.candidate_sha }}"
    )
    if ci_text.count("uses: actions/checkout@") != ci_text.count(
        exact_candidate_ref
    ):
        failures.append(
            "every checkout must use the exact resolved candidate SHA"
        )
    if ci_text.count("runs-on:") != len(
        re.findall(r"(?m)^    timeout-minutes:", ci_text)
    ):
        failures.append("every job must define a timeout")
    media_job_match = re.search(
        r"(?ms)^  postgres-media-integration:\n"
        r"(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        ci_text,
    )
    if media_job_match is None:
        failures.append("postgres-media-integration job is missing")
    else:
        media_job = media_job_match.group("body")
        if len(
            re.findall(r"(?m)^    timeout-minutes:", media_job)
        ) != 1:
            failures.append(
                "postgres-media-integration must define one job timeout"
            )
        if (
            media_job.count("- name: Check out candidate") != 1
            or media_job.count("uses: actions/checkout@") != 1
            or media_job.count(exact_candidate_ref) != 1
        ):
            failures.append(
                "postgres-media-integration must contain one exact "
                "candidate checkout"
            )
    if ci_text.count(f"POSTGRES_IMAGE: {EXPECTED_POSTGRES_IMAGE}") != 1:
        failures.append(
            "the actual-operation PostgreSQL 16 image must have one "
            "exact global pin"
        )
    if any(
        ci_text.count(image) != 1
        for image in EXPECTED_POSTGRES_AUDIT_IMAGES
    ):
        failures.append(
            "each matching-major PostgreSQL audit image must have one "
            "exact matrix pin"
        )
    if (
        ci_text.count(
            "Run real matching-major external-media integration"
        )
        != 1
    ):
        failures.append(
            "ci.yml must define one matching-major media integration matrix"
        )
    if ci_text.count("git ls-files -z -- '*.sh'") < 2:
        failures.append("ci.yml must syntax-check and ShellCheck every tracked shell script")
    if "workers/polytao_worker" in ci_text or "POLYTAO_WORKER_BASE_URL" in ci_text:
        failures.append("ci.yml must not build or test the removed standalone PolyTAO Worker")
    validate_release_input(failures)
    validate_deployment_asset_pin(failures)
    validate_adopted_permission_documentation(failures)
    validate_successor_authority_contract_sources(failures)
    validate_exact_b_bridge(failures)

    if failures:
        for failure in failures:
            print(f"workflow policy: {failure}")
        return 1
    print("validated the single CI/CD workflow and release input")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

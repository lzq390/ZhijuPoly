#!/usr/bin/env python3
"""Dependency-free policy checks for the single NexPoly CI/CD workflow."""

from __future__ import annotations

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
LEGACY_REMOTE_RELEASE_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "remote_release.sh"
EXACT_B_BRIDGE_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "test_exact_b_bridge.sh"
BACKEND_DOCKERFILE_PATH = REPOSITORY_ROOT / "Dockerfile"
BACKEND_IMAGE_ASSERTION_PATH = (
    REPOSITORY_ROOT / "scripts" / "ci" / "assert_backend_image_identity.sh"
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
EXPECTED_B_SHA = "82a69ddb42bcd5c4666b5bf038d02414bccc6dde"
EXPECTED_B_TREE = "44e4b4c398b7b84abdeb40bc02b885569aba4d8b"
EXPECTED_B_BRIDGE_CORE_BLOB = "15b8a1378d4100a5c74666344107bf00661fe34f"
EXPECTED_B_BACKEND_IMAGE = (
    "ghcr.io/lzq390/nexpoly-backend@sha256:"
    "ec850b6873cca0340a63faf47ab19b3c4a65f1a656c5866e73487890a6f057f4"
)
EXPECTED_B_WEB_IMAGE = (
    "ghcr.io/lzq390/nexpoly-web@sha256:"
    "6b7e51ba07861e9894d484e7f0133128697c47fe02c230ab179a38c3d053d008"
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
        label="exact-B pre-0013 mutable digest",
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
                    "exact-B pre-0013 mutable digest has an incomplete or "
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
            label="exact-B pre-0013 mutable digest",
            failures=failures,
        )

    transition = _unique_shell_section(
        text,
        start='b_transition_before="$(pre_dft_mutable_digest "$B_DATABASE")"',
        end=(
            'run_backend_command "$F_BACKEND_IMAGE" "$F_DATABASE" \\\n'
            "  python -m app.postgres_migrations --mode bootstrap"
        ),
        label="exact-B B/post-0012 to F/0013 to B to F transition",
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
                "F applies only expand migrations to B",
                'run_backend_command "$F_BACKEND_IMAGE" "$B_DATABASE" \\\n'
                "  python -m app.postgres_migrations --mode expand",
            ),
            (
                "exact final 0013 ledger",
                "0013_monomer_dft_jobs:"
                "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc",
            ),
            ("post-migration mutable digest", mutable_unchanged),
            (
                "F starts on the transitioned database",
                'start_backend "$F_BACKEND_IMAGE" "$B_DATABASE" '
                '"$b_transition_f_name" 18106',
            ),
            ("F reports schema ready", "assert_dft_state 18106 true"),
            ("post-F mutable digest", mutable_unchanged),
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
            ("post-B mutable digest", mutable_unchanged),
            (
                "F accepts the database after B",
                'run_backend_command "$F_BACKEND_IMAGE" "$B_DATABASE" \\\n'
                "  python -m app.postgres_preflight --mode schema --strict",
            ),
            ("post-return-to-F mutable digest", mutable_unchanged),
        ),
        label="exact-B B/post-0012 to F/0013 to B to F transition",
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
                "Run real B-schema and F/0013-schema transition smoke",
            ),
            ("exact transition script", "run: scripts/ci/test_exact_b_bridge.sh"),
        ),
        label="exact-b-bridge job",
        failures=failures,
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
            "Run real B-schema and F/0013-schema transition smoke",
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
            "sudo apt-get install --yes postgresql-client-16",
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
    validate_gpu_session_compose_policy(ci_text, failures)
    validate_exact_b_job(ci_text, failures)
    validate_backend_image_identity_policy(ci_text, failures)
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

    if ci_text.count("push: true") != 2:
        failures.append("ci.yml must push exactly the Backend and Web SHA images")
    if ci_text.count("ghcr.io/lzq390/nexpoly-backend:sha-") != 2:
        failures.append(
            "ci.yml must publish and finally seal one immutable Backend SHA tag"
        )
    if ci_text.count("ghcr.io/lzq390/nexpoly-web:sha-") != 2:
        failures.append(
            "ci.yml must publish and finally seal one immutable Web SHA tag"
        )
    if ci_text.count(
        "org.opencontainers.image.revision=${{ needs.resolve-sha.outputs.candidate_sha }}"
    ) != 2:
        failures.append("both published images must bind the immutable source revision label")
    if ci_text.count(
        "org.opencontainers.image.source=${{ github.server_url }}/${{ github.repository }}"
    ) != 2:
        failures.append("both published images must bind the repository source label")
    if ci_text.count(
        "org.opencontainers.image.version=sha-${{ needs.resolve-sha.outputs.candidate_sha }}"
    ) != 2:
        failures.append("both published images must bind the immutable SHA version label")
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
    if ci_text.count("runs-on:") != ci_text.count("timeout-minutes:"):
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
        if media_job.count("timeout-minutes:") != 1:
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
    validate_exact_b_bridge(failures)

    if failures:
        for failure in failures:
            print(f"workflow policy: {failure}")
        return 1
    print("validated the single CI/CD workflow and release input")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

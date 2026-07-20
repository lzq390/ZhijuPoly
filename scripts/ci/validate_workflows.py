#!/usr/bin/env python3
"""Dependency-free policy checks for the single NexPoly CI/CD workflow."""

from __future__ import annotations

import json
import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = REPOSITORY_ROOT / ".github" / "workflows"
CI_PATH = WORKFLOW_ROOT / "ci.yml"
RELEASE_INPUT_PATH = REPOSITORY_ROOT / "release-input.json"
DEPLOYMENT_DOC_PATH = REPOSITORY_ROOT / "docs" / "deployment.md"
LEGACY_REMOTE_RELEASE_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "remote_release.sh"
EXACT_B_BRIDGE_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "test_exact_b_bridge.sh"

PINNED_ACTION = re.compile(
    r"^\s*-?\s*uses:\s*[^\s@]+@([0-9a-f]{40})(?:\s*#.*)?$"
)
ANY_ACTION = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)")
WORKFLOW_JOB_HEADER = re.compile(r"(?m)^  (?P<name>[A-Za-z0-9_-]+):\n")
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


def validate_complete_history_checkouts(
    ci_text: str,
    failures: list[str],
) -> None:
    jobs = ("script-tests", "bridge-validation", "exact-b-bridge")
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
            "only script-tests, bridge-validation, and exact-b-bridge may request "
            "complete Git history"
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
            "python -m pytest workers/monomer_md_worker/tests",
            "name: Monomer-DFT Worker tests",
            "python-version: \"3.12\"",
            "workers/monomer_dft_worker/requirements-ci.lock",
            "env -u PYTHONPATH python -m pytest workers/monomer_dft_worker/tests",
            "scripts/tests/test_monomer_md_worker_launcher.py",
            "scripts/tests/test_worker_slot_runtime.py",
            "Rebuild the production Worker runtime lock from empty",
            'python -m venv --clear "$runtime_venv"',
            '"$runtime_venv/bin/python" -m pip install',
            "--require-hashes --only-binary=:all:",
            "workers/monomer_md_worker/requirements.lock",
            '"$runtime_venv/bin/python" -m pip check',
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
    validate_exact_b_job(ci_text, failures)

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
    if ci_text.count("ghcr.io/lzq390/nexpoly-backend:sha-") != 1:
        failures.append("ci.yml must publish exactly one immutable Backend SHA tag")
    if ci_text.count("ghcr.io/lzq390/nexpoly-web:sha-") != 1:
        failures.append("ci.yml must publish exactly one immutable Web SHA tag")
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

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
RELEASE_CONTROLLER_PATH = REPOSITORY_ROOT / "scripts" / "release_controller.py"

PINNED_ACTION = re.compile(
    r"^\s*-?\s*uses:\s*[^\s@]+@([0-9a-f]{40})(?:\s*#.*)?$"
)
ANY_ACTION = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_DATASET = re.compile(r"^[A-Za-z0-9_.-]+$")
EXPECTED_DATASETS = [
    "governance",
    "core",
    "knowledge",
    "online",
    "pi",
    "dft",
    "experimental",
    "lab",
    "property_filter",
]


def require_markers(text: str, markers: tuple[str, ...], failures: list[str]) -> None:
    for marker in markers:
        if marker not in text:
            failures.append(f"ci.yml is missing required control: {marker}")


def validate_release_input(failures: list[str]) -> None:
    try:
        document = json.loads(RELEASE_INPUT_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        failures.append(f"release-input.json is missing or invalid: {exc}")
        return
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "asset_manifest_digest",
        "datasets_on_asset_change",
    }:
        failures.append("release-input.json must contain exactly the reviewed schema fields")
        return
    if document["schema_version"] != 1:
        failures.append("release-input.json schema_version must be 1")
    digest = document["asset_manifest_digest"]
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        failures.append("release-input.json asset_manifest_digest must be an immutable sha256 digest")
    datasets = document["datasets_on_asset_change"]
    if datasets != EXPECTED_DATASETS:
        failures.append(
            "release-input.json datasets_on_asset_change must equal the reviewed explicit dataset order"
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
            "workflow_dispatch:",
            "branches: [main]",
            "DISPATCH_OPERATION",
            "[[ \"$EVENT_REF\" == refs/pull/*/merge ]]",
            "[[ \"$EVENT_REF\" == refs/heads/main ]]",
            "[[ \"$DISPATCH_OPERATION\" == bootstrap ]]",
            "name: ci-gate",
            "  release:\n"
            "    name: Build, smoke, package, and deploy current main\n"
            "    if: >-\n"
            "      !cancelled() &&\n"
            "      needs.ci-gate.result == 'success' &&\n"
            "      (github.event_name == 'push' || github.event_name == 'workflow_dispatch')\n"
            "    needs: [resolve-sha, ci-gate]",
            "python3 scripts/ci/backend_test_shards.py --shards 3 --shard",
            "python -m pytest workers/monomer_md_worker/tests",
            "working-directory: frontend",
            "run: npm test",
            "python3 -m unittest discover -s scripts/tests -p 'test_*.py' -v",
            "python3 scripts/ci/validate_dependency_locks.py",
            "python3 -m app.migration_policy",
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
            "python -m pip download --require-hashes --only-binary=:all:",
            "nexpoly-release-${RELEASE_SHA}.tar.gz",
            "--release-bundle \"$bundle\"",
            "--release-input release-input.json",
            "environment: nexpoly-production",
            "group: nexpoly-production",
            "cancel-in-progress: false",
            "NEXPOLY_SSH_KNOWN_HOSTS: ${{ secrets.NEXPOLY_SSH_KNOWN_HOSTS }}",
            "scripts/ci/remote_release.sh \"$DEPLOY_MODE\"",
            "dist/release-manifest.json",
            "NEXPOLY_PRODUCTION_ROOT: /data/lzq/gith/nexpoly",
            "Reconfirm that this SHA is still current main",
            "Automatic production deployment is disabled during the migration-epoch bridge",
            "steps.deployment-gate.outputs.enabled == 'true'",
        ),
        failures,
    )

    for forbidden in (
        "workflow_run:",
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
    ):
        if forbidden in ci_text:
            failures.append(f"ci.yml contains forbidden legacy/implicit control: {forbidden}")

    if ci_text.count("scripts/ci/remote_release.sh \"$DEPLOY_MODE\"") != 1:
        failures.append("ci.yml must expose exactly one production deployment path")
    if ci_text.count("NEXPOLY_SSH_PRIVATE_KEY: ${{ secrets.NEXPOLY_SSH_PRIVATE_KEY }}") != 1:
        failures.append("the SSH private key must be scoped to the single deployment step")
    if ci_text.count("NEXPOLY_SSH_KNOWN_HOSTS: ${{ secrets.NEXPOLY_SSH_KNOWN_HOSTS }}") != 1:
        failures.append("pinned known_hosts must be scoped to the single deployment step")
    if ci_text.count("uses: actions/checkout@") != ci_text.count("Assert immutable checkout"):
        failures.append("every checkout must be followed by an immutable SHA assertion")
    if ci_text.count("uses: actions/checkout@") != ci_text.count("persist-credentials: false"):
        failures.append("every checkout must disable persisted GitHub credentials")
    if ci_text.count("runs-on:") != ci_text.count("timeout-minutes:"):
        failures.append("every job must define a timeout")
    if ci_text.count("git ls-files -z -- '*.sh'") < 2:
        failures.append("ci.yml must syntax-check and ShellCheck every tracked shell script")
    if "workers/polytao_worker" in ci_text or "POLYTAO_WORKER_BASE_URL" in ci_text:
        failures.append("ci.yml must not build or test the removed standalone PolyTAO Worker")
    try:
        controller_text = RELEASE_CONTROLLER_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        failures.append(f"release controller is unavailable: {exc}")
    else:
        if 'self.run_migrations(environment, mode="bootstrap-expand")' not in controller_text:
            failures.append(
                "production first-release takeover must retain bootstrap-expand"
            )

    validate_release_input(failures)

    if failures:
        for failure in failures:
            print(f"workflow policy: {failure}")
        return 1
    print("validated the single CI/CD workflow and release input")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

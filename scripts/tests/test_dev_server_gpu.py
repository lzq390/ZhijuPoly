from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "dev_server_gpu.sh"
DEV_COMPOSE = REPOSITORY_ROOT / "docker-compose.dev.yml"
GPU_SESSION_COMPOSE = REPOSITORY_ROOT / "docker-compose.dev-gpu-session.yml"
BACKEND_DOCKERFILE = REPOSITORY_ROOT / "Dockerfile"
DEV_ENV_EXAMPLE = REPOSITORY_ROOT / ".env.dev.example"
DEV_BUILDKIT_CONFIG = REPOSITORY_ROOT / "ops" / "config" / "buildkitd.dev.toml"


class DevServerGpuScriptTests(unittest.TestCase):
    def _shell_function_source(self, name: str) -> str:
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index(f"{name}() {{")
        end = source.index("\n}\n", start) + len("\n}\n")
        return source[start:end]

    def _run_gpu_up_rollback(
        self,
        *,
        controller_source: str,
        session_id: str = "a" * 32,
    ) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            controller = root / "controller"
            controller.write_text(controller_source, encoding="utf-8")
            controller.chmod(0o700)
            controller_log = root / "controller.log"
            fallback_log = root / "fallback.log"
            harness = f"""
set -u
GPU_SESSION_PYTHON="$1"
GPU_SESSION_CONTROLLER="ignored-controller-path"
GPU_SESSION_ROLLBACK_ARMED=true
NEXPOLY_DEV_GPU_SESSION_ID="$2"
export FAKE_CONTROLLER_LOG="$3"
FALLBACK_LOG="$4"

{self._shell_function_source("gpu_session_controller_status_fields")}
{self._shell_function_source("gpu_session_controller_owns_recovery")}
{self._shell_function_source("gpu_session_controller_finish_recovery")}
{self._shell_function_source("gpu_session_up_rollback")}

gpu_session_stop_owned_internal() {{
  printf '%s\n' stop-owned >> "$FALLBACK_LOG"
}}
gpu_session_restore_cpu_internal() {{
  printf '%s\n' restore-cpu >> "$FALLBACK_LOG"
}}

set +e
false
gpu_session_up_rollback
"""
            completed = subprocess.run(
                [
                    "bash",
                    "-c",
                    harness,
                    "rollback-harness",
                    str(controller),
                    session_id,
                    str(controller_log),
                    str(fallback_log),
                ],
                text=True,
                capture_output=True,
            )
            controller_calls = (
                controller_log.read_text(encoding="utf-8").splitlines()
                if controller_log.exists()
                else []
            )
            fallback_calls = (
                fallback_log.read_text(encoding="utf-8").splitlines()
                if fallback_log.exists()
                else []
            )
        return completed, controller_calls, fallback_calls

    def _asset_verifier_source(self) -> str:
        lines = SCRIPT.read_text(encoding="utf-8").splitlines()
        marker = 'python3 - "$NEXPOLY_ASSET_ROOT" "$manifest" <<\'PY\''
        start = lines.index(next(line for line in lines if line.strip() == marker)) + 1
        end = lines.index("PY", start)
        return "\n".join(lines[start:end])

    def _asset_fixture(self, root: Path) -> tuple[Path, Path]:
        release = root / "release"
        assets: dict[str, list[dict[str, str | int]]] = {}
        for asset_root in ("model", "database", "backend-data", "byteff2"):
            content = f"{asset_root}-content".encode()
            path = release / asset_root / "nested" / "asset.bin"
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
            assets[asset_root] = [
                {
                    "path": "nested/asset.bin",
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ]
        commit = "a" * 40
        commit_marker = release / "byteff2" / "BYTEFF2-COMMIT"
        commit_marker.write_text(commit + "\n", encoding="ascii")
        marker_content = commit_marker.read_bytes()
        assets["byteff2"].append(
            {
                "path": "BYTEFF2-COMMIT",
                "size": len(marker_content),
                "sha256": hashlib.sha256(marker_content).hexdigest(),
            }
        )
        manifest = release / "ASSET-MANIFEST.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "byteff2_commit": commit,
                    "byteff2_submodules": {},
                    "assets": assets,
                }
            ),
            encoding="utf-8",
        )
        return release, manifest

    def _run_asset_verifier(self, release: Path, manifest: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-", str(release), str(manifest)],
            input=self._asset_verifier_source(),
            text=True,
            capture_output=True,
        )

    def test_script_has_valid_shell_syntax(self) -> None:
        completed = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_embedded_smoke_python_has_valid_syntax(self) -> None:
        lines = SCRIPT.read_text(encoding="utf-8").splitlines()
        blocks: list[str] = []
        current: list[str] | None = None
        for line in lines:
            if line.rstrip().endswith("<<'PY'"):
                current = []
            elif current is not None and line == "PY":
                blocks.append("\n".join(current))
                current = None
            elif current is not None:
                current.append(line)
        self.assertGreaterEqual(len(blocks), 2)
        for index, block in enumerate(blocks):
            compile(block, f"dev_server_gpu.sh embedded Python block {index}", "exec")

    def test_dev_worker_pythonpath_includes_repository_root(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'export PYTHONPATH="$ROOT_DIR:$BYTEFF2_ROOT:'
            '$BYTEFF2_ROOT/submodules/bytemol${PYTHONPATH:+:$PYTHONPATH}"',
            source,
        )

    def test_dev_worker_path_includes_frozen_base_environment(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'export PATH="$(dirname "$WORKER_PYTHON"):'
            '$(dirname "$WORKER_BASE_PYTHON"):$PATH"',
            source,
        )

    def test_asset_validation_delegates_schema_v2_to_authoritative_contract(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("from scripts.asset_release_contract import (", source)
        self.assertIn("validate_schema_v2_release(", source)
        self.assertIn('expected_digest=f"sha256:{release_root.name}"', source)

    def test_backend_build_uses_the_default_compose_builder(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('DEV_PYPI_INDEX_URL="${NEXPOLY_DEV_PYPI_INDEX_URL:-https://pypi.org/simple}"', source)
        self.assertIn('DEV_PYPI_MIRROR_URL="${NEXPOLY_DEV_PYPI_MIRROR_URL:-https://mirrors.ustc.edu.cn/pypi/simple}"', source)
        self.assertIn('"${COMPOSE[@]}" build', source)
        self.assertIn("--builder default", source)
        self.assertIn('--build-arg SOURCE_REVISION="$NEXPOLY_BUILD_REVISION"', source)
        self.assertIn('--build-arg PYPI_INDEX_URL="$DEV_PYPI_INDEX_URL"', source)
        self.assertIn('--build-arg PYPI_MIRROR_URL="$DEV_PYPI_MIRROR_URL"', source)
        self.assertNotIn("docker buildx create", source)
        self.assertNotIn("docker buildx use", source)
        self.assertIn("docker buildx inspect default", source)
        self.assertIn("docker buildx rm nexpoly-dev-safe", source)
        self.assertNotIn("DEV_BUILDKIT", source)
        self.assertNotIn("DEV_BUILDX", source)
        self.assertIn('docker image inspect "$DEV_BACKEND_IMAGE" >/dev/null', source)
        build_start = source.index("build_backend_image() {")
        build_end = source.index("\n}\n", build_start)
        self.assertIn("assert_clean_candidate", source[build_start:build_end])
        drift_start = source.index("verify_backend_drift() {")
        drift_end = source.index("\n}\n", drift_start)
        self.assertIn("assert_clean_candidate", source[drift_start:drift_end])
        self.assertIn("git ls-files --others --exclude-standard", source)

    def test_backend_tests_use_a_dedicated_locked_image_and_full_discovery(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        compose = DEV_COMPOSE.read_text(encoding="utf-8")
        dockerfile = BACKEND_DOCKERFILE.read_text(encoding="utf-8")

        self.assertIn("FROM backend-base AS backend-test", dockerfile)
        self.assertIn("COPY backend/requirements-ci.lock", dockerfile)
        self.assertIn("FROM backend-base AS runtime", dockerfile)
        self.assertIn("backend-test:", compose)
        self.assertIn("target: backend-test", compose)

        branch = source[
            source.index("  test-backend)"):source.index("  build-frontend)")
        ]
        self.assertIn("test_backend", branch)
        test_function = source[
            source.index("test_backend() ("):source.index("smoke_static()")
        ]
        self.assertIn("--profile test up -d backend-test-postgres", test_function)
        self.assertIn("build --builder default backend-test", test_function)
        self.assertIn("run --rm --no-deps backend-test", test_function)
        self.assertIn("python -m pytest /app/backend/tests", test_function)
        self.assertIn("rm -sf backend-test-postgres", test_function)
        self.assertNotIn("tests/test_", branch)

    def test_ordinary_up_never_auto_applies_the_contract(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index("run_dev_migrations() {")
        body = source[start:source.index("\n}\n\nrun_dev_contract_migration()", start)]
        self.assertNotIn('--mode contract', body)
        self.assertIn("Destructive migration 0012 is pending", body)
        up = source[source.index('case "${1:-up}" in'):]
        up_branch = up[up.index("  up)"):up.index("  stop)")]
        self.assertIn("run_dev_migrations", up_branch)
        self.assertNotIn("run_dev_contract_migration", up_branch)

    def test_ordinary_up_precreates_owner_private_worker_mounts(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        case = source[source.index('case "${1:-up}" in'):]
        up_branch = case[case.index("  up)"):case.index("  stop)")]

        self.assertIn("prepare_worker_runtime_directories", up_branch)
        self.assertIn("prepare_dft_runtime_directories", up_branch)
        self.assertLess(
            up_branch.index("prepare_worker_runtime_directories"),
            up_branch.index("build_backend_image"),
        )
        self.assertLess(
            up_branch.index("prepare_dft_runtime_directories"),
            up_branch.index("build_backend_image"),
        )

    def test_explicit_contract_command_archives_full_database_and_removed_table(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index("run_dev_contract_migration() {")
        body = source[start:source.index("\n}\n\ncompute_backend_config_hash()", start)]
        self.assertIn("nexpoly_dev.full.dump", body)
        self.assertIn("polytao_jobs.dump", body)
        self.assertIn("SELECT COUNT(*) FROM generation.polytao_jobs", body)
        self.assertIn("source_schema_migration_version", body)
        self.assertIn("sha256sum", body)
        self.assertIn("createdb", body)
        self.assertIn("pg_restore --exit-on-error", body)
        self.assertIn("restored_row_count", body)
        self.assertIn("dropdb --if-exists", body)
        self.assertIn("python -m app.postgres_migrations --mode contract", body)
        self.assertIn("  contract-migrate)", source)
        contract_branch = source[
            source.index("  contract-migrate)"):source.index("  tunnel)")
        ]
        self.assertLess(contract_branch.index("validate_asset_release"), contract_branch.index("build_backend_image"))
        self.assertLess(contract_branch.index("build_backend_image"), contract_branch.index("run_dev_contract_migration"))

    def test_dedicated_buildx_configuration_is_removed(self) -> None:
        env_example = DEV_ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertFalse(DEV_BUILDKIT_CONFIG.exists())
        self.assertNotIn("NEXPOLY_DEV_BUILDX", env_example)
        self.assertNotIn("NEXPOLY_DEV_BUILDKIT", env_example)

    def test_builder_flow_never_prunes_docker_state(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for destructive_command in (
            "docker buildx prune",
            "docker builder prune",
            "docker image prune",
            "docker system prune",
        ):
            self.assertNotIn(destructive_command, source)

    def test_gpu_session_device_is_literal_gpu1_and_env_override_is_rejected(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        overlay = (REPOSITORY_ROOT / "docker-compose.dev-gpu-session.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('[[ "${NEXPOLY_DEV_GPU_DEVICE:-1}" == "1" ]]', source)
        self.assertIn('export NEXPOLY_DEV_GPU_DEVICE=1', source)
        self.assertIn('GPU_SESSION_PYTHON="/usr/bin/python3"', source)
        self.assertIn('- "1"', overlay)
        self.assertIn(
            'NVIDIA_VISIBLE_DEVICES: "none"',
            DEV_COMPOSE.read_text(encoding="utf-8"),
        )
        self.assertIn('NVIDIA_VISIBLE_DEVICES: "1"', overlay)
        self.assertIn("'NVIDIA_VISIBLE_DEVICES':'none'", source)
        self.assertIn("'NVIDIA_VISIBLE_DEVICES':'1'", source)
        self.assertNotIn("NEXPOLY_DEV_GPU_DEVICE", overlay)

    def test_gpu_session_activation_is_ordered_after_real_dft_residency(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index("gpu_session_up() {")
        body = source[start:source.index("\n}\n\ngpu_session_status()", start)]
        ordered_markers = (
            "dft_worker_ctl start",
            'dft_health="$(curl',
            'dft_worker_session_record bind "$dft_health"',
            "stabilize --execute",
            "worker_up",
            '"${GPU_COMPOSE[@]}" up -d --no-deps --force-recreate backend',
            "wait_gpu_backend_configured",
            "write_gpu_session_activation_manifest",
            "activate --execute",
        )
        positions = [body.index(marker) for marker in ordered_markers]
        self.assertEqual(positions, sorted(positions))

        down_start = source.index("gpu_session_down() {")
        down_body = source[
            down_start:
            source.index("\n}\n\ngpu_session_stop_owned_internal()", down_start)
        ]
        self.assertIn('"stabilizing"', down_body)
        self.assertIn('if [[ "$state" == "stopped" ]]', down_body)
        self.assertIn("verify_gpu_session_stopped_runtime", down_body)
        self.assertGreaterEqual(
            down_body.count("verify_gpu_session_stopped_runtime"),
            2,
        )
        self.assertLess(
            down_body.index('if [[ "$state" == "stopped" ]]'),
            down_body.index("NEXPOLY_DEV_GPU_SESSION_ID="),
        )

        stopped_start = source.index("verify_gpu_session_stopped_runtime() {")
        stopped_body = source[
            stopped_start:source.index("\n}\n\ngpu_session_status()", stopped_start)
        ]
        self.assertIn("verify_backend_drift", stopped_body)
        for marker in (
            ".runtime/gpu-session/controller.json",
            ".runtime/gpu-resource/broker.sock",
            ".runtime/gpu-resource/mps-1",
            '"$WORKER_PID_FILE"',
            '"$WORKER_SOCKET"',
            '"$DFT_WORKER_SESSION_RECORD"',
            ".runtime/monomer-dft-worker.pid",
            '"$DFT_WORKER_SOCKET_DIR/worker.sock"',
        ):
            self.assertIn(marker, stopped_body)

    def test_gpu_session_up_pins_the_exact_backend_candidate_identity(self) -> None:
        body = self._shell_function_source("gpu_session_up")
        expected_calls = (
            'gpu_backend_candidate_hash="$(compute_gpu_backend_config_hash)"',
            'NEXPOLY_DEV_CONFIG_HASH="$gpu_backend_candidate_hash"',
            '"${GPU_COMPOSE[@]}" up -d --no-deps --force-recreate backend',
            'gpu_backend_candidate_id="$("${GPU_COMPOSE[@]}" ps -q backend)"',
            'wait_gpu_backend_configured "$gpu_backend_candidate_id" "$gpu_backend_candidate_hash"',
            'verify_gpu_backend_drift plane-ready "$gpu_backend_candidate_id" "$gpu_backend_candidate_hash"',
            'write_gpu_session_activation_manifest "$gpu_backend_candidate_id" "$gpu_backend_candidate_hash"',
            'verify_gpu_backend_drift ready "$gpu_backend_candidate_id" "$gpu_backend_candidate_hash"',
        )
        positions = [body.index(marker) for marker in expected_calls]
        self.assertEqual(positions, sorted(positions))

        verification = self._shell_function_source("verify_gpu_backend_drift")
        self.assertIn('desired_hash="$expected_config_hash"', verification)
        self.assertIn('container_id" != "$expected_container_id', verification)
        manifest = self._shell_function_source("write_gpu_session_activation_manifest")
        self.assertIn('container_id" != "$expected_container_id', manifest)

        stop = self._shell_function_source("gpu_backend_stop_exact_session")
        self.assertLess(
            stop.index('archive_gpu_backend_candidate_logs "$container_id"'),
            stop.index('"${GPU_COMPOSE[@]}" stop backend'),
        )

    def test_gpu_backend_wait_rejects_a_same_name_cpu_replacement(self) -> None:
        candidate = "a" * 64
        replacement = "b" * 64
        config_hash = "c" * 64
        session_id = "d" * 32
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            compose_count = root / "compose-count"
            archive_log = root / "archive.log"
            compose_count.write_text("0", encoding="ascii")
            harness = f"""
set -u
EXPECTED_ID="$1"
REPLACEMENT_ID="$2"
EXPECTED_HASH="$3"
NEXPOLY_DEV_GPU_SESSION_ID="$4"
COUNT_FILE="$5"
ARCHIVE_LOG="$6"
GPU_COMPOSE=(fake_compose)

{self._shell_function_source("gpu_backend_candidate_identity_lost")}
{self._shell_function_source("wait_gpu_backend_configured")}

fake_compose() {{
  local count
  [[ "$*" == "ps -q backend" ]] || return 9
  count="$(<"$COUNT_FILE")"
  if [[ "$count" == "0" ]]; then
    printf '1' > "$COUNT_FILE"
    printf '%s\n' "$EXPECTED_ID"
  else
    printf '%s\n' "$REPLACEMENT_ID"
  fi
}}
docker() {{
  [[ "$1" == "inspect" && "$2" == "-f" ]] || return 8
  case "$3" in
    *com.nexpoly.gpu.session-id*) printf '%s\n' "$NEXPOLY_DEV_GPU_SESSION_ID" ;;
    *com.nexpoly.dev.config-hash*) printf '%s\n' "$EXPECTED_HASH" ;;
    *State.Status*) printf '%s\n' running ;;
    *State.Restarting*) printf '%s\n' false ;;
    *RestartCount*) printf '%s\n' 0 ;;
    *State.Health*) printf '%s\n' starting ;;
    *) return 7 ;;
  esac
}}
archive_gpu_backend_candidate_logs() {{
  printf '%s\n' "$1" >> "$ARCHIVE_LOG"
}}
sleep() {{ :; }}

set +e
wait_gpu_backend_configured "$EXPECTED_ID" "$EXPECTED_HASH"
result=$?
set -e
printf 'result=%s\n' "$result"
"""
            completed = subprocess.run(
                [
                    "bash",
                    "-c",
                    harness,
                    "wait-replacement-harness",
                    candidate,
                    replacement,
                    config_hash,
                    session_id,
                    str(compose_count),
                    str(archive_log),
                ],
                text=True,
                capture_output=True,
            )
            archived = (
                archive_log.read_text(encoding="ascii").splitlines()
                if archive_log.exists()
                else []
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("result=1", completed.stdout)
        self.assertIn("candidate identity lost", completed.stderr)
        self.assertIn("controller recovery or a same-name replacement", completed.stderr)
        self.assertEqual(archived, [candidate])

    def test_gpu_backend_wait_archives_the_first_restart_failure(self) -> None:
        candidate = "a" * 64
        config_hash = "c" * 64
        session_id = "d" * 32
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive_log = root / "archive.log"
            sleep_log = root / "sleep.log"
            harness = f"""
set -u
EXPECTED_ID="$1"
EXPECTED_HASH="$2"
NEXPOLY_DEV_GPU_SESSION_ID="$3"
ARCHIVE_LOG="$4"
SLEEP_LOG="$5"
GPU_COMPOSE=(fake_compose)

{self._shell_function_source("gpu_backend_candidate_identity_lost")}
{self._shell_function_source("wait_gpu_backend_configured")}

fake_compose() {{
  [[ "$*" == "ps -q backend" ]] || return 9
  printf '%s\n' "$EXPECTED_ID"
}}
docker() {{
  if [[ "$1" == "logs" ]]; then
    printf '%s\n' 'native crash evidence'
    return 0
  fi
  [[ "$1" == "inspect" && "$2" == "-f" ]] || return 8
  case "$3" in
    *com.nexpoly.gpu.session-id*) printf '%s\n' "$NEXPOLY_DEV_GPU_SESSION_ID" ;;
    *com.nexpoly.dev.config-hash*) printf '%s\n' "$EXPECTED_HASH" ;;
    *State.Status*) printf '%s\n' restarting ;;
    *State.Restarting*) printf '%s\n' true ;;
    *RestartCount*) printf '%s\n' 1 ;;
    *) return 7 ;;
  esac
}}
archive_gpu_backend_candidate_logs() {{
  printf '%s\n' "$1" >> "$ARCHIVE_LOG"
}}
sleep() {{ printf '%s\n' slept >> "$SLEEP_LOG"; }}

set +e
wait_gpu_backend_configured "$EXPECTED_ID" "$EXPECTED_HASH"
result=$?
set -e
printf 'result=%s\n' "$result"
"""
            completed = subprocess.run(
                [
                    "bash",
                    "-c",
                    harness,
                    "wait-restart-harness",
                    candidate,
                    config_hash,
                    session_id,
                    str(archive_log),
                    str(sleep_log),
                ],
                text=True,
                capture_output=True,
            )
            archived = archive_log.read_text(encoding="ascii").splitlines()
            slept = sleep_log.exists()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("result=1", completed.stdout)
        self.assertIn("native/restart failure", completed.stderr)
        self.assertIn("restart_count=1", completed.stderr)
        self.assertEqual(archived, [candidate])
        self.assertFalse(slept)

    def test_gpu_backend_verify_rejects_a_replacement_before_drift_checks(self) -> None:
        candidate = "a" * 64
        replacement = "b" * 64
        config_hash = "c" * 64
        script_source = SCRIPT.read_text(encoding="utf-8")
        verify_start = script_source.index("verify_gpu_backend_drift() {")
        verify_identity_prefix = (
            script_source[
                verify_start:script_source.index(
                    '  expected_image="$(docker image inspect', verify_start
                )
            ]
            + "}\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            archive_log = Path(raw) / "archive.log"
            harness = f"""
set -u
EXPECTED_ID="$1"
REPLACEMENT_ID="$2"
EXPECTED_HASH="$3"
ARCHIVE_LOG="$4"
GPU_COMPOSE=(fake_compose)

{self._shell_function_source("gpu_backend_candidate_identity_lost")}
{verify_identity_prefix}

assert_clean_candidate() {{ :; }}
assert_default_builder() {{ :; }}
fake_compose() {{
  [[ "$*" == "ps -q backend" ]] || return 9
  printf '%s\n' "$REPLACEMENT_ID"
}}
archive_gpu_backend_candidate_logs() {{
  printf '%s\n' "$1" >> "$ARCHIVE_LOG"
}}

set +e
verify_gpu_backend_drift plane-ready "$EXPECTED_ID" "$EXPECTED_HASH"
result=$?
set -e
printf 'result=%s\n' "$result"
"""
            completed = subprocess.run(
                [
                    "bash",
                    "-c",
                    harness,
                    "verify-replacement-harness",
                    candidate,
                    replacement,
                    config_hash,
                    str(archive_log),
                ],
                text=True,
                capture_output=True,
            )
            archived = (
                archive_log.read_text(encoding="ascii").splitlines()
                if archive_log.exists()
                else []
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("result=1", completed.stdout)
        self.assertIn("candidate identity lost", completed.stderr)
        self.assertNotIn("Compose configuration has drifted", completed.stderr)
        self.assertEqual(archived, [candidate])

    def test_gpu_backend_log_archive_is_private_and_never_blocks_recovery(self) -> None:
        candidate = "a" * 64
        session_id = "d" * 32
        with tempfile.TemporaryDirectory() as raw:
            run_directory = Path(raw) / f"20260722T010203Z-{session_id}"
            run_directory.mkdir(mode=0o700)
            harness = f"""
set -u
EXPECTED_ID="$1"
NEXPOLY_DEV_GPU_SESSION_ID="$2"
RUN_DIRECTORY="$3"

{self._shell_function_source("archive_gpu_backend_candidate_logs")}

gpu_session_current_run_directory() {{
  printf '%s\n' "$RUN_DIRECTORY"
}}
docker() {{
  if [[ "$1" == "inspect" && "$2" == "-f" ]]; then
    printf '%s\n' "$NEXPOLY_DEV_GPU_SESSION_ID"
    return 0
  fi
  if [[ "$1" == "logs" ]]; then
    printf '%s\n' 'candidate stdout'
    printf '%s\n' 'candidate stderr' >&2
    return 7
  fi
  return 8
}}

archive_gpu_backend_candidate_logs "$EXPECTED_ID"
printf 'result=%s\n' "$?"
"""
            completed = subprocess.run(
                [
                    "bash",
                    "-c",
                    harness,
                    "archive-harness",
                    candidate,
                    session_id,
                    str(run_directory),
                ],
                text=True,
                capture_output=True,
            )
            archive = run_directory / f"backend-candidate-{candidate}.log"
            contents = archive.read_text(encoding="utf-8") if archive.exists() else ""
            mode = archive.stat().st_mode & 0o777 if archive.exists() else None

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("result=0", completed.stdout)
        self.assertEqual(mode, 0o600)
        self.assertIn("candidate stdout", contents)
        self.assertIn("candidate stderr", contents)
        archive_source = self._shell_function_source("archive_gpu_backend_candidate_logs")
        self.assertNotIn(".Config.Env", archive_source)

    def test_gpu_up_rollback_defers_to_live_controller_automatic_recovery(self) -> None:
        completed, controller_calls, fallback_calls = self._run_gpu_up_rollback(
            controller_source="""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_CONTROLLER_LOG"
if [[ "$*" == *" status" ]]; then
  printf '%s\\n' '{"status":"contaminated","session_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
  exit 0
fi
if [[ "$*" == *" down --execute" ]]; then
  exit 0
fi
exit 9
""",
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(fallback_calls, [])
        self.assertEqual(
            controller_calls,
            [
                "-I ignored-controller-path status",
                "-I ignored-controller-path down --execute",
            ],
        )

    def test_gpu_up_rollback_keeps_exact_shell_fallback_if_controller_is_dead(
        self,
    ) -> None:
        completed, controller_calls, fallback_calls = self._run_gpu_up_rollback(
            controller_source="""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_CONTROLLER_LOG"
exit 7
""",
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(fallback_calls, ["stop-owned", "restore-cpu"])
        self.assertEqual(
            controller_calls,
            [
                "-I ignored-controller-path status",
                "-I ignored-controller-path drain --execute",
                "-I ignored-controller-path status",
                "-I ignored-controller-path down --execute",
            ],
        )

    def test_gpu_up_rollback_rechecks_controller_ownership_after_drain(
        self,
    ) -> None:
        completed, controller_calls, fallback_calls = self._run_gpu_up_rollback(
            controller_source="""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_CONTROLLER_LOG"
if [[ "$*" == *" status" ]]; then
  status_count="$(grep -c ' status$' "$FAKE_CONTROLLER_LOG")"
  if [[ "$status_count" == "1" ]]; then
    printf '%s\\n' '{"status":"plane-ready","session_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
  else
    printf '%s\\n' '{"status":"contaminated","session_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
  fi
  exit 0
fi
if [[ "$*" == *" drain --execute" || "$*" == *" down --execute" ]]; then
  exit 0
fi
exit 9
""",
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(fallback_calls, [])
        self.assertEqual(
            controller_calls,
            [
                "-I ignored-controller-path status",
                "-I ignored-controller-path drain --execute",
                "-I ignored-controller-path status",
                "-I ignored-controller-path down --execute",
            ],
        )

    def test_gpu_up_rollback_keeps_shell_fallback_if_controller_never_takes_over(
        self,
    ) -> None:
        completed, controller_calls, fallback_calls = self._run_gpu_up_rollback(
            controller_source="""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_CONTROLLER_LOG"
if [[ "$*" == *" status" ]]; then
  printf '%s\\n' '{"status":"plane-ready","session_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
  exit 0
fi
if [[ "$*" == *" drain --execute" || "$*" == *" down --execute" ]]; then
  exit 0
fi
exit 9
""",
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(fallback_calls, ["stop-owned", "restore-cpu"])
        self.assertEqual(
            controller_calls,
            [
                "-I ignored-controller-path status",
                "-I ignored-controller-path drain --execute",
                "-I ignored-controller-path status",
                "-I ignored-controller-path down --execute",
            ],
        )

    def test_gpu_up_rollback_never_takes_over_after_live_controller_wait_timeout(
        self,
    ) -> None:
        completed, controller_calls, fallback_calls = self._run_gpu_up_rollback(
            controller_source="""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_CONTROLLER_LOG"
if [[ "$*" == *" status" ]]; then
  printf '%s\\n' '{"status":"cleanup-blocked","session_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
  exit 0
fi
exit 8
""",
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(fallback_calls, [])
        self.assertIn("remains the recovery authority", completed.stderr)
        self.assertEqual(
            controller_calls,
            [
                "-I ignored-controller-path status",
                "-I ignored-controller-path down --execute",
                "-I ignored-controller-path status",
            ],
        )

    def test_gpu_up_rollback_takes_over_only_after_recovery_controller_dies(
        self,
    ) -> None:
        completed, controller_calls, fallback_calls = self._run_gpu_up_rollback(
            controller_source="""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_CONTROLLER_LOG"
if [[ "$*" == *" status" ]]; then
  status_count="$(grep -c ' status$' "$FAKE_CONTROLLER_LOG")"
  if [[ "$status_count" == "1" ]]; then
    printf '%s\\n' '{"status":"audit-failed","session_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
    exit 0
  fi
fi
exit 7
""",
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(fallback_calls, ["stop-owned", "restore-cpu"])
        self.assertEqual(
            controller_calls,
            [
                "-I ignored-controller-path status",
                "-I ignored-controller-path down --execute",
                "-I ignored-controller-path status",
                "-I ignored-controller-path drain --execute",
                "-I ignored-controller-path status",
                "-I ignored-controller-path down --execute",
            ],
        )

    def test_canary_state_is_dev_private_and_fenced_from_production(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        compose = DEV_COMPOSE.read_text(encoding="utf-8")
        self.assertIn("MONOMER_MD_DEV_CANARY_STATE_DIR", source)
        self.assertIn("MONOMER_MD_DEV_CANARY_STATE_DIR", compose)
        self.assertIn(
            "/data/lzq/gith/nexpoly-runtime/*",
            source,
        )
        self.assertIn('[[ ! -L "$CANARY_STATE_DIR" ]]', source)
        self.assertIn('chmod 700 "$CANARY_STATE_DIR"', source)

    def test_loaded_tag_matches_compose_and_drift_verification(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        compose = DEV_COMPOSE.read_text(encoding="utf-8")
        self.assertIn('DEV_BACKEND_IMAGE="nexpoly-dev-backend:latest"', source)
        self.assertIn('BACKEND_URL="http://127.0.0.1:${NEXPOLY_DEV_BACKEND_PORT:-18000}"', source)
        self.assertIn('backend_base + "/internal/gpu/status"', source)
        self.assertIn('"${COMPOSE[@]}" build', source)
        self.assertIn('docker image inspect -f \'{{.Id}}\' "$DEV_BACKEND_IMAGE"', source)
        self.assertIn('org.opencontainers.image.revision', source)
        self.assertIn('NEXPOLY_BUILD_REVISION" == "$CURRENT_SOURCE_REVISION', source)
        self.assertIn('runtime_revision=', source)
        self.assertIn('image_revision=', source)
        self.assertIn("compute_backend_config_hash", source)
        self.assertIn('com.nexpoly.dev.config-hash', source)
        self.assertIn('com.nexpoly.dev.config-hash', compose)
        self.assertIn('GPU_MAX_CONCURRENT_INFERENCES: "1"', compose)
        self.assertIn('GPU_MAX_WAITING_INFERENCES: "8"', compose)
        self.assertIn('GEN_MAX_ACTIVE_JOBS: "8"', compose)
        self.assertIn("'GPU_MAX_CONCURRENT_INFERENCES':'1'", source)
        self.assertIn("'WEB_CONCURRENCY':'1'", source)
        self.assertGreaterEqual(compose.count("image: nexpoly-dev-backend:latest"), 2)

    def test_asset_manifest_verifier_accepts_exact_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            release, manifest = self._asset_fixture(Path(raw))
            completed = self._run_asset_verifier(release, manifest)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_asset_manifest_verifier_rejects_hash_mismatch_and_unlisted_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            release, manifest = self._asset_fixture(Path(raw))
            (release / "model" / "nested" / "asset.bin").write_bytes(b"model-CONTENT")
            mismatch = self._run_asset_verifier(release, manifest)
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("sha256 mismatch", mismatch.stderr)

            release, manifest = self._asset_fixture(Path(raw) / "second")
            (release / "database" / "extra.bin").write_bytes(b"extra")
            unlisted = self._run_asset_verifier(release, manifest)
            self.assertNotEqual(unlisted.returncode, 0)
            self.assertIn("unlisted asset file", unlisted.stderr)

    def test_asset_manifest_verifier_rejects_path_traversal_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            release, manifest = self._asset_fixture(Path(raw))
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["assets"]["model"][0]["path"] = "../outside.bin"
            manifest.write_text(json.dumps(document), encoding="utf-8")
            traversal = self._run_asset_verifier(release, manifest)
            self.assertNotEqual(traversal.returncode, 0)
            self.assertIn("unsafe manifest path", traversal.stderr)

            release, manifest = self._asset_fixture(Path(raw) / "second")
            asset = release / "model" / "nested" / "asset.bin"
            outside = Path(raw) / "outside.bin"
            outside.write_bytes(asset.read_bytes())
            asset.unlink()
            asset.symlink_to(outside)
            symlink = self._run_asset_verifier(release, manifest)
            self.assertNotEqual(symlink.returncode, 0)
            self.assertIn("symlink is not allowed", symlink.stderr)

    def test_dev_reload_watches_only_the_application_directory(self) -> None:
        compose = DEV_COMPOSE.read_text(encoding="utf-8")
        self.assertIn("--reload-dir", compose)
        self.assertIn("/app/backend/app", compose)
        self.assertIn("cd /app/backend/app", compose)
        self.assertIn("--app-dir /app/backend", compose)
        self.assertNotIn("--reload-exclude", compose)

    def test_gpu_session_backend_keeps_stable_lease_process_identity(self) -> None:
        compose = GPU_SESSION_COMPOSE.read_text(encoding="utf-8")
        self.assertIn("exec python -X faulthandler -m uvicorn app.main:app", compose)
        self.assertIn("--workers 1", compose)
        self.assertIn("ipc: host", compose)
        self.assertNotIn("--reload", compose)

    def test_worker_bootstrap_and_start_are_fail_closed(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        env_example = DEV_ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("worker_prepare_venv()", source)
        self.assertIn("scripts/prepare_dev_worker_venv.py prepare", source)
        prepare_source = (REPOSITORY_ROOT / "scripts" / "prepare_dev_worker_venv.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"--require-hashes"', prepare_source)
        self.assertIn("  worker-base-identity)", source)
        self.assertIn("  worker-venv)", source)
        self.assertIn("worker_verify_venv", source)
        self.assertIn("worker_assert_process_identity", source)
        self.assertIn("worker_cleanup_failed_launch()", source)
        self.assertIn("worker_process_record collect-dead", source)
        self.assertIn("worker_secure_socket()", source)
        self.assertIn("stat -c '%u:%a'", source)
        worker_start = source.index("worker_up() {")
        worker_up = source[worker_start:source.index("\n}\n\nworker_stop()", worker_start)]
        self.assertLess(worker_up.index("validate_asset_release"), worker_up.index("worker_health"))
        self.assertLess(worker_up.index("worker_verify_venv"), worker_up.index("worker_health"))
        self.assertIn("export MONOMER_MD_GPU_SCOPE_LAUNCHER=systemd-user-scope", worker_up)
        self.assertIn("MONOMER_MD_DEV_WORKER_BASE_PYTHON=", env_example)
        self.assertIn("MONOMER_MD_DEV_WORKER_BASE_PYTHON_IDENTITY_SHA256=sha256:", env_example)


if __name__ == "__main__":
    unittest.main()

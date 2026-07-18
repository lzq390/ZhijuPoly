from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.monomer-dft-dev.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.monomer-dft.dev.example"
CONTROL_SCRIPT = REPO_ROOT / "scripts" / "monomer_dft_dev_stack.sh"
WORKER_CONTROL_SCRIPT = REPO_ROOT / "scripts" / "monomer_dft_worker_ctl.sh"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
GITIGNORE = REPO_ROOT / ".gitignore"
NGINX_CONFIG = REPO_ROOT / "nginx.conf"
PRODUCTION_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"


def _run_control_functions(
    body: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = f"source {shlex.quote(str(CONTROL_SCRIPT))}\n{body}"
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["bash", "-c", command],
        cwd=REPO_ROOT,
        env=merged_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_worker_control_functions(body: str) -> subprocess.CompletedProcess[str]:
    command = f"source {shlex.quote(str(WORKER_CONTROL_SCRIPT))}\n{body}"
    return subprocess.run(
        ["bash", "-c", command],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )


def _compose_config(project_name: str = "nexpoly_dft_dev") -> dict:
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            project_name,
            "--env-file",
            str(ENV_EXAMPLE),
            "--file",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


@unittest.skipIf(shutil.which("docker") is None, "docker CLI is not installed")
class ComposeIsolationTests(unittest.TestCase):
    def test_compose_is_fully_namespaced_and_has_fixed_bindings(self) -> None:
        config = _compose_config()

        self.assertEqual(config["name"], "nexpoly_dft_dev")
        self.assertEqual(set(config["services"]), {"postgres", "migrate", "backend", "frontend"})
        self.assertEqual(
            config["services"]["postgres"]["image"],
            "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777",
        )
        self.assertEqual(config["services"]["backend"]["image"], "nexpoly-dft-dev-backend:latest")
        self.assertEqual(config["services"]["frontend"]["image"], "nexpoly-dft-dev-frontend:latest")

        bindings = {
            name: {
                (port.get("host_ip"), int(port["published"]), int(port["target"]))
                for port in service.get("ports", [])
            }
            for name, service in config["services"].items()
        }
        self.assertEqual(bindings["frontend"], {("127.0.0.1", 25173, 80)})
        self.assertEqual(bindings["backend"], {("127.0.0.1", 28000, 8000)})
        self.assertEqual(bindings["postgres"], {("127.0.0.1", 25532, 5432)})
        self.assertEqual(bindings["migrate"], set())

        self.assertEqual(
            config["services"]["migrate"]["command"],
            ["python", "-m", "app.postgres_migrations", "--mode", "bootstrap"],
        )

        volume_names = {volume["name"] for volume in config["volumes"].values()}
        self.assertEqual(volume_names, {"nexpoly_dft_dev_monomer_dft_postgres_data"})
        self.assertEqual(config["networks"]["default"]["name"], "nexpoly_dft_dev_default")

    def test_explicit_fresh_project_gets_a_distinct_postgres_volume(self) -> None:
        config = _compose_config("nexpoly_dft_fresh_0013")

        self.assertEqual(config["name"], "nexpoly_dft_fresh_0013")
        self.assertEqual(
            {volume["name"] for volume in config["volumes"].values()},
            {"nexpoly_dft_fresh_0013_monomer_dft_postgres_data"},
        )
        self.assertEqual(
            config["networks"]["default"]["name"],
            "nexpoly_dft_fresh_0013_default",
        )

    def test_backend_has_no_gpu_and_only_mounts_private_dft_runtime(self) -> None:
        config = _compose_config()
        backend = config["services"]["backend"]

        self.assertNotIn("gpus", backend)
        self.assertNotIn("deploy", backend)
        volumes = {volume["target"]: volume for volume in backend["volumes"]}
        self.assertEqual(
            set(volumes),
            {
                "/app/monomer-dft-worker",
                "/app/.runtime/monomer-dft-download-spool",
            },
        )
        self.assertEqual(
            volumes["/app/monomer-dft-worker"]["source"],
            str(REPO_ROOT / ".runtime" / "monomer-dft-worker-socket"),
        )
        self.assertTrue(volumes["/app/monomer-dft-worker"]["read_only"])
        self.assertEqual(
            volumes["/app/.runtime/monomer-dft-download-spool"]["source"],
            str(REPO_ROOT / ".runtime" / "monomer-dft-download-spool"),
        )
        self.assertFalse(
            volumes["/app/.runtime/monomer-dft-download-spool"].get(
                "read_only", False
            )
        )
        for volume in volumes.values():
            self.assertEqual(
                volume.get("bind", {}).get("create_host_path"),
                False,
            )

        environment = backend["environment"]
        for name in (
            "MODEL_ENABLED",
            "OCSR_ENABLED",
            "GEN_MODEL_ENABLED",
            "POLYTAO_ENABLED",
            "RETRO_MODEL_ENABLED",
            "SMIPOLY_ENABLED",
            "MONOMER_MD_SUBMIT_ENABLED",
        ):
            self.assertEqual(environment[name], "false")
        self.assertEqual(environment["MONOMER_DFT_SUBMIT_ENABLED"], "true")
        self.assertEqual(environment["MONOMER_DFT_WORKER_UDS"], "/app/monomer-dft-worker/worker.sock")
        self.assertEqual(environment["MONOMER_DFT_VALIDATION_CONCURRENCY"], "2")
        self.assertEqual(environment["MONOMER_DFT_DOWNLOAD_MAX_CONCURRENT"], "2")
        self.assertEqual(
            environment["MONOMER_DFT_DOWNLOAD_SPOOL_ROOT"],
            "/app/.runtime/monomer-dft-download-spool",
        )


class ControlScriptSafetyTests(unittest.TestCase):
    def test_host_runtime_and_real_environment_are_outside_the_build_context(self) -> None:
        patterns = {
            line.strip()
            for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertTrue(
            {".runtime/", "/.runtime/"} & patterns,
            patterns,
        )
        self.assertIn(".env*", patterns)

        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", ".env.monomer-dft.dev"],
            cwd=REPO_ROOT,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        self.assertIn(
            ".env.monomer-dft.dev",
            GITIGNORE.read_text(encoding="utf-8").splitlines(),
        )

    def test_production_delivery_keeps_dft_submission_and_socket_disabled(self) -> None:
        compose = PRODUCTION_COMPOSE.read_text(encoding="utf-8")

        self.assertIn('MONOMER_DFT_SUBMIT_ENABLED: "false"', compose)
        self.assertIn('MONOMER_DFT_WORKER_UDS: ""', compose)
        self.assertNotIn("monomer-dft-worker-socket:/app/monomer-dft-worker", compose)

    def test_nginx_dft_proxy_never_buffers_verified_downloads(self) -> None:
        nginx = NGINX_CONFIG.read_text(encoding="utf-8")
        location_start = nginx.index("location ^~ /api/v1/monomer-dft/")
        generic_start = nginx.index("location /api/", location_start)
        dft_location = nginx[location_start:generic_start]

        self.assertIn("proxy_buffering off;", dft_location)
        self.assertIn("proxy_request_buffering off;", dft_location)
        self.assertIn("proxy_max_temp_file_size 0;", dft_location)
        self.assertIn("proxy_pass_header X-Accel-Buffering;", dft_location)

    def test_control_script_never_prunes_or_deletes_volumes(self) -> None:
        script = CONTROL_SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn("docker system prune", script)
        self.assertNotIn("down --volumes", script)
        self.assertNotIn("down -v", script)
        self.assertIn("compose down --remove-orphans", script)
        self.assertIn("worker_request POST /drain", script)
        self.assertIn("assert_full_stack_gate", script)
        self.assertIn("0012_drop_polytao_jobs.sql", script)
        self.assertIn("python3 -m app.migration_policy", script)
        self.assertIn('payload.get("schema_version") != 2', script)
        self.assertIn('contract.get("kind") != "contract"', script)
        self.assertIn('dft.get("kind") != "expand"', script)
        self.assertIn('dft.get("requires_contracts")', script)
        self.assertIn("restart) assert_full_stack_gate; stop_stack; start_stack", script)
        self.assertIn("assert_worker_ready", script)
        self.assertIn("assert_worker_draining", script)
        self.assertIn('payload.get("status") != "draining"', script)
        self.assertIn("worker_instance_id", script)
        self.assertIn("worker instance changed during drain", script)
        self.assertIn("worker drain state was lost", script)
        self.assertIn("worker_instance_is_draining", script)
        self.assertIn("deadline=$((SECONDS + timeout))", script)
        self.assertIn("wait_for_worker_quiescence", script)
        self.assertIn("stop-if-drained-instance", script)
        self.assertIn('running_job_count "$expected_worker_instance_id"', script)
        self.assertIn('payload.get("jobs")', script)
        self.assertIn("total != len(jobs)", script)
        self.assertIn("(( timeout >= 1 ))", script)
        self.assertIn('MONOMER_DFT_WORKER_SOCKET_DIR="$socket_dir"', script)
        self.assertIn('NEXPOLY_DFT_FRONTEND_PORT:-25173', script)
        self.assertIn('NEXPOLY_DFT_PROJECT_NAME:-nexpoly_dft_dev', script)
        self.assertIn('volume: ${PROJECT_NAME}_monomer_dft_postgres_data', script)
        self.assertIn('DOWNLOAD_SPOOL_DIR="$REPO_ROOT/.runtime/', script)
        self.assertIn("ensure_download_spool", script)

    def test_worker_controller_rejects_prod_gpu0_gpu2_and_production_state(self) -> None:
        cases = (
            (
                "MONOMER_DFT_DEPLOYMENT=prod; "
                "NEXPOLY_DFT_GPU_DEVICE=1; "
                "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES=3; "
                "validate_dev_selection",
                "must be exactly dev",
            ),
            (
                "MONOMER_DFT_DEPLOYMENT=dev; "
                "NEXPOLY_DFT_GPU_DEVICE=0; "
                "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES=3; "
                "validate_dev_selection",
                "GPUs 0 and 2 are forbidden",
            ),
            (
                "MONOMER_DFT_DEPLOYMENT=dev; "
                "NEXPOLY_DFT_GPU_DEVICE=1; "
                "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES=2; "
                "validate_dev_selection",
                "GPUs 0 and 2 are forbidden",
            ),
            (
                "MONOMER_DFT_DEPLOYMENT=dev; "
                "NEXPOLY_DFT_GPU_DEVICE=1; "
                "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES=3; "
                "MONOMER_DFT_GPU_BROKER_ENABLED=1; "
                "MONOMER_DFT_STANDALONE_GPU_SMOKE=0; "
                "MONOMER_DFT_GPU_BROKER_UDS="
                "/data/lzq/gith/nexpoly/ops/state/gpu-resource/broker.sock; "
                "configure_paths",
                "must be below",
            ),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                completed = _run_worker_control_functions(body)
                self.assertEqual(
                    completed.returncode,
                    2,
                    completed.stdout + completed.stderr,
                )
                self.assertIn(expected, completed.stderr)

    def test_delivery_scripts_have_no_production_execution_path(self) -> None:
        for relative in (
            "scripts/monomer_dft_dev_stack.sh",
            "scripts/monomer_dft_worker_ctl.sh",
            "scripts/preflight_monomer_dft_env.py",
            "scripts/setup_monomer_dft_env.sh",
            "scripts/smoke_monomer_dft_env.py",
        ):
            with self.subTest(relative=relative):
                text = (REPO_ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("/data/lzq/gith/nexpoly/ops/", text)
                self.assertNotIn("/data/lzq/nexpoly-assets/", text)
                self.assertNotIn("remote_release.sh", text)

    def test_download_spool_is_private_and_worktree_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fake_repo = Path(raw)
            runtime = fake_repo / ".runtime"
            runtime.mkdir(mode=0o700)
            spool = runtime / "monomer-dft-download-spool"
            completed = _run_control_functions(
                f"""
REPO_ROOT={shlex.quote(str(fake_repo))}
DOWNLOAD_SPOOL_DIR={shlex.quote(str(spool))}
ensure_download_spool
stat -c '%a' "$DOWNLOAD_SPOOL_DIR"
"""
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            self.assertEqual(completed.stdout.strip(), "700")
            self.assertTrue(spool.is_dir())

    def test_ready_runtime_does_not_confuse_full_queue_with_failure(self) -> None:
        completed = _run_control_functions(
            """
worker_request() {
  printf '%s\n' '{"status":"ok","runtime_ready":true,"accepting_jobs":false,"draining":false,"recovering":false}'
}
assert_worker_ready
"""
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_start_failure_redrains_an_existing_resumed_worker(self) -> None:
        worker_instance_id = "a" * 32
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "drained"
            completed = _run_control_functions(
                f"""
assert_full_stack_gate() {{ :; }}
ensure_download_spool() {{ :; }}
worker_running() {{ return 0; }}
worker_request() {{
  if [[ "$1 $2" == "GET /health" ]]; then
    printf '%s\n' '{{"draining":true}}'
  else
    printf '%s\n' '{{}}'
  fi
}}
worker_instance_id() {{ printf '%s\n' {shlex.quote(worker_instance_id)}; }}
assert_worker_ready() {{ return 1; }}
drain_worker_instance() {{
  : > {shlex.quote(str(marker))}
  printf '%s\n' {shlex.quote(worker_instance_id)}
}}
worker_instance_is_draining() {{ [[ "$1" == {shlex.quote(worker_instance_id)} ]]; }}
start_stack
"""
            )

            self.assertEqual(completed.returncode, 2)
            self.assertTrue(marker.exists(), completed.stdout + completed.stderr)
            self.assertIn("worker runtime is not ready", completed.stderr)

    def test_final_stop_redrains_replacement_and_uses_instance_fence(self) -> None:
        old_instance_id = "a" * 32
        new_instance_id = "b" * 32
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            stopped = temp_root / "stopped"
            calls = temp_root / "calls"
            drains = temp_root / "drains"
            worker_ctl = temp_root / "worker-ctl"
            worker_ctl.write_text(
                "#!/bin/sh\n"
                'printf "%s %s\\n" "$1" "$2" > "$CALLS"\n'
                f'test "$1" = stop-if-drained-instance\n'
                f'test "$2" = {new_instance_id}\n'
                ': > "$STOPPED"\n',
                encoding="utf-8",
            )
            worker_ctl.chmod(0o700)
            completed = _run_control_functions(
                f"""
WORKER_CTL={shlex.quote(str(worker_ctl))}
worker_running() {{ [[ ! -e "$STOPPED" ]]; }}
worker_instance_id() {{ printf '%s\n' {shlex.quote(new_instance_id)}; }}
worker_instance_is_draining() {{ [[ "$1" == {shlex.quote(new_instance_id)} ]]; }}
drain_worker_instance() {{
  printf '%s\n' drain >> "$DRAINS"
  printf '%s\n' {shlex.quote(new_instance_id)}
}}
running_job_count() {{ printf '0\n'; }}
sleep() {{ :; }}
stop_worker_fenced {shlex.quote(old_instance_id)} 2
""",
                env={
                    "STOPPED": str(stopped),
                    "CALLS": str(calls),
                    "DRAINS": str(drains),
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertTrue(stopped.exists())
            self.assertEqual(
                calls.read_text(encoding="utf-8").strip(),
                f"stop-if-drained-instance {new_instance_id}",
            )
            self.assertEqual(drains.read_text(encoding="utf-8").strip(), "drain")

    def test_untracked_gate_files_cannot_open_the_full_stack(self) -> None:
        relative_files = (
            "backend/migrations/postgres/0012_drop_polytao_jobs.sql",
            "backend/migrations/postgres/0013_monomer_dft_jobs.sql",
            "backend/migrations/postgres/manifest.json",
            "backend/app/migration_policy.py",
            ".github/workflows/ci.yml",
            "scripts/release_controller.py",
            "backend/app/services/deployment_control.py",
            "scripts/tests/test_release_controller.py",
            "backend/tests/test_deployment_control.py",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_repo = Path(temp_dir)
            subprocess.run(["git", "init", "--quiet", str(fake_repo)], check=True)
            for relative_file in relative_files:
                path = fake_repo / relative_file
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            completed = _run_control_functions(
                f"""
REPO_ROOT={shlex.quote(str(fake_repo))}
MIGRATIONS_DIR="$REPO_ROOT/backend/migrations/postgres"
assert_full_stack_gate
"""
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("must be committed", completed.stderr)

    def test_temporary_workflow_removal_must_be_committed(self) -> None:
        relative_files = (
            "backend/migrations/postgres/0012_drop_polytao_jobs.sql",
            "backend/migrations/postgres/0013_monomer_dft_jobs.sql",
            "backend/migrations/postgres/manifest.json",
            "backend/app/migration_policy.py",
            ".github/workflows/ci.yml",
            ".github/workflows/monomer-dft-ci.yml",
            "scripts/validate_monomer_dft_release_contract.py",
            "scripts/release_controller.py",
            "backend/app/services/deployment_control.py",
            "scripts/tests/test_release_controller.py",
            "backend/tests/test_deployment_control.py",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_repo = Path(temp_dir)
            subprocess.run(["git", "init", "--quiet", str(fake_repo)], check=True)
            for relative_file in relative_files:
                path = fake_repo / relative_file
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(fake_repo), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(fake_repo),
                    "-c",
                    "user.name=Codex Test",
                    "-c",
                    "user.email=codex@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ],
                check=True,
            )
            (fake_repo / ".github/workflows/monomer-dft-ci.yml").unlink()
            completed = _run_control_functions(
                f"""
REPO_ROOT={shlex.quote(str(fake_repo))}
MIGRATIONS_DIR="$REPO_ROOT/backend/migrations/postgres"
assert_full_stack_gate
"""
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("removal must be committed", completed.stderr)

    def test_conditional_controller_validates_the_complete_quiescent_fence(self) -> None:
        worker_instance_id = "c" * 32
        invalid_payloads = (
            {
                "worker_instance_id": "d" * 32,
                "draining": True,
                "accepting_jobs": False,
                "active_jobs": 0,
                "recovering": False,
            },
            {
                "worker_instance_id": worker_instance_id,
                "draining": False,
                "accepting_jobs": True,
                "active_jobs": 0,
                "recovering": False,
            },
            {
                "worker_instance_id": worker_instance_id,
                "draining": True,
                "accepting_jobs": False,
                "active_jobs": 1,
                "recovering": False,
            },
            {
                "worker_instance_id": worker_instance_id,
                "draining": True,
                "accepting_jobs": False,
                "active_jobs": 0,
                "recovering": True,
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                completed = _run_worker_control_functions(
                    f"""
socket_health() {{ printf '%s\n' {shlex.quote(json.dumps(payload))}; }}
if assert_expected_drained_instance {shlex.quote(worker_instance_id)}; then
  exit 0
fi
exit 9
"""
                )
                self.assertEqual(completed.returncode, 9, completed.stdout + completed.stderr)

        valid_payload = {
            "worker_instance_id": worker_instance_id,
            "draining": True,
            "accepting_jobs": False,
            "active_jobs": 0,
            "recovering": False,
        }
        completed = _run_worker_control_functions(
            f"""
socket_health() {{ printf '%s\n' {shlex.quote(json.dumps(valid_payload))}; }}
assert_expected_drained_instance {shlex.quote(worker_instance_id)}
"""
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_conditional_stop_refuses_to_signal_an_active_worker(self) -> None:
        worker_instance_id = "e" * 32
        payload = {
            "worker_instance_id": worker_instance_id,
            "draining": True,
            "accepting_jobs": False,
            "active_jobs": 1,
            "recovering": False,
        }
        completed = _run_worker_control_functions(
            f"""
read_pid_state() {{ MANAGED_PID=123; MANAGED_START_TICKS=456; return 0; }}
process_is_running() {{ return 0; }}
socket_health() {{ printf '%s\n' {shlex.quote(json.dumps(payload))}; }}
terminate_verified_process() {{ printf 'unsafe signal\n'; return 0; }}
stop_worker {shlex.quote(worker_instance_id)}
"""
        )

        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("unsafe signal", completed.stdout)
        self.assertIn("does not match the drained instance fence", completed.stderr)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_HELPER = REPO_ROOT / "scripts" / "monomer_worker_env.py"
STATUS_PROBE = REPO_ROOT / "scripts" / "monomer_worker_status_probe.py"
GPU_IDLE_CHECK = REPO_ROOT / "scripts" / "monomer_gpu_idle_check.py"
PROTOCOLS_PROBE = REPO_ROOT / "scripts" / "monomer_backend_protocols_probe.py"
STATE_HELPER = REPO_ROOT / "scripts" / "deploy_state.py"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_server.sh"
PROVISION_SCRIPT = REPO_ROOT / "scripts" / "provision_monomer_md_worker_venv.sh"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "nexpoly-deploy.yml"
RUN_HOST_WORKER = REPO_ROOT / "workers" / "monomer_md_worker" / "run_host_worker.sh"
SYSTEMD_UNIT = REPO_ROOT / "ops" / "systemd" / "nexpoly-monomer-md-worker.service"


def ready_health(**overrides: object) -> bytes:
    value: dict[str, object] = {
        "status": "ok",
        "runtime_ready": True,
        "active_jobs": 0,
        "accepting_jobs": True,
        "draining": False,
        "protocols": {
            "Transport": {
                "supported": True,
                "runtime_ready": True,
                "runtime_error": None,
            }
        },
    }
    value.update(overrides)
    return json.dumps(value).encode()


class WorkerEnvironmentHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env_file = self.root / "worker.env"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_env(self, text: str, mode: int = 0o600) -> None:
        self.env_file.write_text(text, encoding="utf-8")
        self.env_file.chmod(mode)

    def helper(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ENV_HELPER), *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=check,
        )

    def test_validate_get_and_exec_use_literal_values(self) -> None:
        sentinel = self.root / "must-not-exist"
        literal = f"postgresql://user:$(touch {sentinel})@127.0.0.1/db"
        self.write_env(
            "MONOMER_MD_PYTHON=/runtime/current/bin/python\n"
            f"APP_POSTGRES_DSN={literal}\n"
            "BYTEFF2_ROOT=/srv/byteff2\n"
            "MONOMER_MD_JOB_ROOT=/srv/jobs\n"
        )

        self.helper("validate", str(self.env_file), check=True)
        fetched = self.helper("get", str(self.env_file), "APP_POSTGRES_DSN", check=True)
        executed = self.helper(
            "exec",
            str(self.env_file),
            "--",
            sys.executable,
            "-c",
            "import os; print(os.environ['APP_POSTGRES_DSN'])",
            check=True,
        )

        self.assertEqual(fetched.stdout, literal)
        self.assertEqual(executed.stdout.strip(), literal)
        self.assertFalse(sentinel.exists())

    def test_rejects_reserved_duplicate_unknown_and_nonliteral_keys(self) -> None:
        invalid_values = [
            "MONOMER_MD_REQUIRE_TRANSPORT_READY=false\n",
            "NEXPOLY_MONOMER_MD_ENV_SANITIZED=1\n",
            "BYTEFF2_ROOT=/one\nBYTEFF2_ROOT=/two\n",
            ";not-a-comment\n",
            "PATH=/tmp\n",
            'BYTEFF2_ROOT="/srv/byteff2"\n',
            "export BYTEFF2_ROOT=/srv/byteff2\n",
            "BYTEFF2_UNKNOWN_FUTURE_KEY=value\n",
            "MONOMER_MD_UNKNOWN_FUTURE_KEY=value\n",
            "BYTEFF2_ROOT= /srv/byteff2\n",
            "BYTEFF2_ROOT=/srv/byteff2 \n",
            "BYTEFF2_ROOT=/srv/byteff2\x00suffix\n",
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                self.write_env(value)
                completed = self.helper("validate", str(self.env_file))
                self.assertNotEqual(completed.returncode, 0)
                self.assertNotIn(value.strip().split("=", 1)[-1], completed.stderr)

    def test_requires_mode_0600_regular_non_symlink_owned_file(self) -> None:
        self.write_env("BYTEFF2_ROOT=/srv/byteff2\n", mode=0o644)
        completed = self.helper("validate", str(self.env_file))
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("0600", completed.stderr)

        target = self.root / "target.env"
        target.write_text("BYTEFF2_ROOT=/srv/byteff2\n", encoding="utf-8")
        target.chmod(0o600)
        self.env_file.unlink()
        self.env_file.symlink_to(target)
        completed = self.helper("validate", str(self.env_file))
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("non-symlink", completed.stderr)

    def test_exec_scrubs_governed_keys_omitted_from_environment_file(self) -> None:
        self.write_env(
            "BYTEFF2_ROOT=/srv/byteff2\n"
            "BYTEFF2_PYTHON=/opt/byteff2/bin/python\n"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "BYTEFF2_OPENMM_DIR": "/inherited/unsafe/openmm",
                "MONOMER_MD_CUDA_VISIBLE_DEVICES": "0",
                "MONOMER_MD_REQUIRE_TRANSPORT_READY": "true",
                "CUDA_VISIBLE_DEVICES": "7",
                "NEXPOLY_MONOMER_MD_ENV_SANITIZED": "inherited-bypass",
                "BASH_FUNC_injected%%": "() { echo unsafe; }",
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(ENV_HELPER),
                "exec",
                str(self.env_file),
                "--",
                sys.executable,
                "-c",
                (
                    "import json,os; print(json.dumps({"
                    "'present':[name for name in "
                    "('BYTEFF2_OPENMM_DIR','MONOMER_MD_CUDA_VISIBLE_DEVICES',"
                    "'MONOMER_MD_REQUIRE_TRANSPORT_READY','CUDA_VISIBLE_DEVICES',"
                    "'BASH_FUNC_injected%%') if name in os.environ],"
                    "'path':os.environ['PATH'],"
                    "'marker':os.environ['NEXPOLY_MONOMER_MD_ENV_SANITIZED']}))"
                ),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["present"], [])
        self.assertEqual(
            payload["path"],
            "/opt/byteff2/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        )
        self.assertEqual(payload["marker"], "1")

    def test_main_lifecycle_worker_settings_are_allowlisted(self) -> None:
        keys = (
            "MONOMER_MD_WORKER_INSTANCE_ID_COLUMN",
            "MONOMER_MD_HEARTBEAT_AT_COLUMN",
            "MONOMER_MD_LEASE_EXPIRES_AT_COLUMN",
            "MONOMER_MD_HEARTBEAT_INTERVAL_SECONDS",
            "MONOMER_MD_LEASE_SECONDS",
            "MONOMER_MD_RECOVERY_RETRY_SECONDS",
        )
        self.write_env("".join(f"{key}=value\n" for key in keys))
        completed = self.helper("validate", str(self.env_file))
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_exec_scrubs_bash_trace_controls_before_starting_shell(self) -> None:
        sentinel = self.root / "must-not-run"
        secret = "DO_NOT_TRACE_DATABASE_SECRET"
        self.write_env(f"APP_POSTGRES_DSN=postgresql://user:{secret}@host/db\n")
        environment = os.environ.copy()
        environment.update(
            {
                "BASHOPTS": "extdebug",
                "BASH_XTRACEFD": "2",
                "PS4": f"$(touch {sentinel}) {secret} ",
                "SHELLOPTS": "xtrace",
            }
        )

        completed = subprocess.run(
            [
                sys.executable,
                str(ENV_HELPER),
                "exec",
                str(self.env_file),
                "--",
                "/bin/bash",
                "-c",
                '[[ -n "${APP_POSTGRES_DSN:-}" ]] && printf safe',
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "safe")
        self.assertNotIn(secret, completed.stderr)
        self.assertFalse(sentinel.exists())


class WorkerStatusProbeTests(unittest.TestCase):
    def probe(
        self,
        payload: bytes,
        *,
        require_transport: bool = False,
        drain_check: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        command = [sys.executable, str(STATUS_PROBE)]
        if require_transport:
            command.append("--require-transport-ready")
        if drain_check:
            command.append("--drain-check")
        return subprocess.run(command, input=payload, capture_output=True, check=False)

    def test_strict_ready_summary_is_allowlisted(self) -> None:
        sentinel = "DO_NOT_LOG_SECRET_SENTINEL"
        payload = ready_health(message=sentinel, runtime_error=sentinel)
        completed = self.probe(payload, require_transport=True)

        self.assertEqual(completed.returncode, 0)
        self.assertNotIn(sentinel.encode(), completed.stdout + completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertTrue(summary["valid_payload"])
        self.assertEqual(summary["status"], "ok")
        self.assertTrue(summary["runtime_ready"])
        self.assertEqual(summary["active_jobs"], 0)
        self.assertTrue(summary["transport"]["runtime_ready"])

    def test_strict_probe_fails_closed_without_leaking_transport_error(self) -> None:
        sentinel = "DO_NOT_LOG_SECRET_SENTINEL"
        payload = ready_health(
            protocols={
                "Transport": {
                    "supported": True,
                    "runtime_ready": False,
                    "runtime_error": sentinel,
                }
            }
        )
        completed = self.probe(payload, require_transport=True)

        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn(sentinel.encode(), completed.stdout + completed.stderr)

    def test_payload_limit_is_fail_closed_and_safe(self) -> None:
        completed = self.probe(b"x" * (64 * 1024 + 1))
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout)["error_category"], "response_too_large")

    def test_drain_check_accepts_degraded_idle_but_rejects_active_jobs(self) -> None:
        degraded_idle = self.probe(
            ready_health(
                status="degraded",
                runtime_ready=False,
                active_jobs=0,
                draining=True,
                accepting_jobs=False,
            ),
            drain_check=True,
        )
        active = self.probe(
            ready_health(
                status="degraded",
                runtime_ready=False,
                active_jobs=1,
                draining=True,
                accepting_jobs=False,
            ),
            drain_check=True,
        )
        accepting = self.probe(
            ready_health(
                status="degraded",
                runtime_ready=False,
                active_jobs=0,
                draining=True,
                accepting_jobs=True,
            ),
            drain_check=True,
        )

        self.assertEqual(degraded_idle.returncode, 0)
        self.assertEqual(json.loads(degraded_idle.stdout)["status"], "degraded")
        self.assertNotEqual(active.returncode, 0)
        self.assertNotEqual(accepting.returncode, 0)


class WorkerGpuIdleGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fake_smi = self.root / "nvidia-smi"
        self.fake_smi.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$1\" in\n"
            "  --query-gpu=*) printf '0, GPU-zero\\n1, GPU-one\\n' ;;\n"
            "  --query-compute-apps=*) printf '%s' \"${FAKE_COMPUTE_APPS:-}\" ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        self.fake_smi.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def gate(
        self, device: str, compute_apps: str = "", *, allow_pid: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["FAKE_COMPUTE_APPS"] = compute_apps
        command = [
            sys.executable,
            str(GPU_IDLE_CHECK),
            "--device-spec",
            device,
            "--nvidia-smi",
            str(self.fake_smi),
        ]
        if allow_pid is not None:
            command.extend(("--allow-pid", allow_pid))
        return subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_only_processes_on_the_selected_gpu_block(self) -> None:
        other_gpu = self.gate("1", "101, GPU-zero\n")
        selected_gpu = self.gate("1", "202, GPU-one\n")

        self.assertEqual(other_gpu.returncode, 0)
        self.assertTrue(json.loads(other_gpu.stdout)["idle"])
        self.assertNotEqual(selected_gpu.returncode, 0)
        self.assertEqual(json.loads(selected_gpu.stdout)["error_category"], "gpu_busy")
        self.assertEqual(json.loads(selected_gpu.stdout)["occupied_processes"], 1)

    def test_only_the_confirmed_existing_worker_pid_is_exempt(self) -> None:
        existing_worker_only = self.gate(
            "1", "202, GPU-one\n", allow_pid="202"
        )
        foreign_process = self.gate(
            "1", "202, GPU-one\n303, GPU-one\n", allow_pid="202"
        )

        self.assertEqual(existing_worker_only.returncode, 0)
        self.assertEqual(
            json.loads(existing_worker_only.stdout)["allowed_worker_processes"], 1
        )
        self.assertNotEqual(foreign_process.returncode, 0)
        self.assertEqual(json.loads(foreign_process.stdout)["occupied_processes"], 1)

    def test_missing_nvidia_smi_fails_closed(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(GPU_IDLE_CHECK),
                "--device-spec",
                "1",
                "--nvidia-smi",
                str(self.root / "missing"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout)["error_category"], "nvidia_smi_missing")


class BackendProtocolsProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        curl = self.root / "curl"
        curl.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n200' \"$FAKE_PROTOCOLS_PAYLOAD\"\n",
            encoding="utf-8",
        )
        curl.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def probe(self, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{self.root}:{environment.get('PATH', '')}"
        environment["FAKE_PROTOCOLS_PAYLOAD"] = json.dumps(payload)
        return subprocess.run(
            [
                sys.executable,
                str(PROTOCOLS_PROBE),
                "--url",
                "http://127.0.0.1:9000/api/v1/monomer-md/protocols",
                "--timeout-seconds",
                "1",
                "--retries",
                "0",
                "--require-transport-ready",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_protocols_endpoint_requires_strict_transport_shape(self) -> None:
        ready = self.probe(
            {
                "enabled": True,
                "available": True,
                "protocols": [
                    {
                        "protocol": "Transport",
                        "supported": True,
                        "runtime_ready": True,
                        "runtime_error": None,
                    }
                ],
            }
        )
        missing_error_field = self.probe(
            {
                "enabled": True,
                "available": True,
                "protocols": [
                    {
                        "protocol": "Transport",
                        "supported": True,
                        "runtime_ready": True,
                    }
                ],
            }
        )

        self.assertEqual(ready.returncode, 0, ready.stderr)
        self.assertTrue(json.loads(ready.stdout)["transport"]["runtime_ready"])
        self.assertNotEqual(missing_error_field.returncode, 0)

    def test_protocols_probe_never_logs_runtime_error(self) -> None:
        sentinel = "DO_NOT_LOG_NATIVE_PATH_SENTINEL"
        completed = self.probe(
            {
                "enabled": True,
                "available": True,
                "protocols": [
                    {
                        "protocol": "Transport",
                        "supported": True,
                        "runtime_ready": False,
                        "runtime_error": sentinel,
                    }
                ],
            }
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn(sentinel, completed.stdout + completed.stderr)


class DeployContractTests(unittest.TestCase):
    def source_and_run(self, command: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(overrides)
        return subprocess.run(
            ["bash", "-c", f'source "{DEPLOY_SCRIPT}"; {command}'],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_transport_required_fails_when_worker_is_disabled(self) -> None:
        completed = self.source_and_run(
            "validate_monomer_release_contract",
            MONOMER_MD_REQUIRE_TRANSPORT_READY="true",
            NEXPOLY_MONOMER_MD_WORKER_MODE="false",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("requires the monomer MD Worker to be enabled", completed.stderr)
        self.assertNotIn("Building Docker images", completed.stdout)

    def test_transport_required_fails_when_worker_env_is_missing(self) -> None:
        missing = "/tmp/definitely-missing-nexpoly-worker.env"
        completed = self.source_and_run(
            "validate_monomer_release_contract",
            MONOMER_MD_REQUIRE_TRANSPORT_READY="true",
            NEXPOLY_MONOMER_MD_WORKER_MODE="true",
            NEXPOLY_MONOMER_MD_WORKER_ENV_FILE=missing,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("requires", completed.stderr)

    def test_required_worker_values_fail_before_candidate_preflight_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            byteff2 = root / "byteff2"
            byteff2.mkdir()
            for empty_key in ("APP_POSTGRES_DSN", "MONOMER_MD_JOB_ROOT"):
                with self.subTest(empty_key=empty_key):
                    values = {
                        "MONOMER_MD_PYTHON": f"{runtime}/current/bin/python",
                        "APP_POSTGRES_DSN": "postgresql://localhost/db",
                        "BYTEFF2_ROOT": str(byteff2),
                        "BYTEFF2_PYTHON": str(Path(sys.executable).resolve()),
                        "MONOMER_MD_JOB_ROOT": str(root / "jobs"),
                    }
                    values[empty_key] = ""
                    env_file = root / f"{empty_key}.env"
                    env_file.write_text(
                        "".join(f"{key}={value}\n" for key, value in values.items()),
                        encoding="utf-8",
                    )
                    env_file.chmod(0o600)
                    completed = self.source_and_run(
                        "validate_monomer_release_contract",
                        NEXPOLY_MONOMER_MD_WORKER_MODE="true",
                        NEXPOLY_MONOMER_MD_WORKER_ENV_FILE=str(env_file),
                        NEXPOLY_MONOMER_MD_VENV_ROOT=str(runtime),
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn(empty_key, completed.stderr)
                    self.assertNotIn("Running candidate", completed.stdout)

    def test_deploy_script_neither_sources_worker_env_nor_runs_pip(self) -> None:
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertNotRegex(script, r"source\s+.*MONOMER_MD_WORKER_ENV_FILE")
        self.assertNotIn('"$MONOMER_MD_PYTHON" -m pip', script)
        self.assertIn("readonly DEPLOY_TRANSPORT_REQUIRED", script)
        self.assertIn('"$CANDIDATE_WORKER_VENV/bin/python"', script)
        self.assertEqual(script.count("check_monomer_gpu_idle "), 2)
        self.assertIn("--disable\n    --fail", script)
        self.assertEqual(script.count("curl --disable --fail"), 2)

    def test_enabled_worker_health_unreachable_fails_closed(self) -> None:
        completed = self.source_and_run(
            "PREVIOUS_WORKER_PID=$$; capture_worker_pid_identity \"$PREVIOUS_WORKER_PID\"; "
            "PREVIOUS_WORKER_ACTIVE=true; monomer_worker_request() { return 1; }; "
            "MONOMER_MD_WORKER_DEPLOY_MODE=true; "
            "drain_monomer_worker_for_preflight",
            NEXPOLY_MONOMER_MD_DRAIN_TIMEOUT_SECONDS="1",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("active jobs cannot be ruled out", completed.stderr)

    def test_degraded_worker_can_drain_and_resume(self) -> None:
        completed = self.source_and_run(
            "PREVIOUS_WORKER_PID=$$; capture_worker_pid_identity \"$PREVIOUS_WORKER_PID\"; "
            "PREVIOUS_WORKER_ACTIVE=true; MONOMER_MD_WORKER_DEPLOY_MODE=true; "
            "monomer_worker_request() { "
            "  if [[ \"$1 $2\" == \"GET /health\" ]]; then "
            "    printf '%s' '{\"status\":\"degraded\",\"runtime_ready\":false,\"active_jobs\":0,\"draining\":true,\"accepting_jobs\":false}'; "
            "  else return 0; fi; "
            "}; "
            "drain_monomer_worker_for_preflight; "
            "[[ \"$MONOMER_WORKER_DRAINED\" == true ]]; "
            "resume_monomer_worker_after_failed_preflight; "
            "[[ \"$MONOMER_WORKER_DRAINED\" == false ]]",
            NEXPOLY_MONOMER_MD_DRAIN_TIMEOUT_SECONDS="5",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_lost_drain_response_still_attempts_identity_guarded_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            resume_marker = Path(temporary) / "resume-attempted"
            completed = self.source_and_run(
                "PREVIOUS_WORKER_PID=$$; capture_worker_pid_identity \"$PREVIOUS_WORKER_PID\"; "
                "PREVIOUS_WORKER_ACTIVE=true; MONOMER_MD_WORKER_DEPLOY_MODE=true; "
                "monomer_worker_request() { "
                "  case \"$1 $2\" in "
                "    \"GET /health\") printf '%s' '{\"status\":\"ok\"}' ;; "
                "    \"POST /drain\") return 28 ;; "
                "    \"POST /resume\") : > \"$RESUME_MARKER\" ;; "
                "    *) return 1 ;; "
                "  esac; "
                "}; "
                "drain_monomer_worker_for_preflight",
                NEXPOLY_MONOMER_MD_DRAIN_TIMEOUT_SECONDS="5",
                RESUME_MARKER=str(resume_marker),
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertTrue(resume_marker.exists(), completed.stderr)

    def test_production_worker_mode_dry_run_fails_before_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            byteff2 = root / "byteff2"
            byteff2.mkdir()
            env_file = root / "worker.env"
            env_file.write_text(
                f"MONOMER_MD_PYTHON={runtime}/current/bin/python\n"
                "MONOMER_MD_WORKER_MODE=dry-run\n"
                "APP_POSTGRES_DSN=postgresql://localhost/db\n"
                f"BYTEFF2_ROOT={byteff2}\n"
                f"BYTEFF2_PYTHON={Path(sys.executable).resolve()}\n"
                f"MONOMER_MD_JOB_ROOT={root / 'jobs'}\n",
                encoding="utf-8",
            )
            env_file.chmod(0o600)
            completed = self.source_and_run(
                "validate_monomer_release_contract",
                NEXPOLY_MONOMER_MD_WORKER_MODE="true",
                NEXPOLY_MONOMER_MD_WORKER_ENV_FILE=str(env_file),
                NEXPOLY_MONOMER_MD_WORKER_PID_FILE=str(root / "worker.pid"),
                NEXPOLY_MONOMER_MD_WORKER_OWNER_FILE=str(root / "worker.owner.json"),
                NEXPOLY_MONOMER_MD_WORKER_LOG_FILE=str(root / "worker.log"),
                NEXPOLY_MONOMER_MD_VENV_ROOT=str(runtime),
                XDG_RUNTIME_DIR=str(root / "no-user-systemd"),
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("MONOMER_MD_WORKER_MODE=real", completed.stderr)
            self.assertNotIn("Running candidate", completed.stdout)

    def test_redrain_occurs_immediately_before_venv_activation(self) -> None:
        completed = self.source_and_run(
            "drain_calls=0; "
            "set_deploy_phase() { :; }; docker() { :; }; "
            "assert_previous_worker_identity_stable() { :; }; "
            "drain_monomer_worker_for_preflight() { drain_calls=$((drain_calls + 1)); }; "
            "activate_candidate_worker_venv() { [[ $drain_calls -eq 1 ]]; }; "
            "restart_monomer_worker() { :; }; wait_for_service_healthy() { :; }; "
            "check_monomer_backend_status() { :; }; run_monomer_md_smoke() { :; }; "
            "check_polytao_backend_status() { :; }; curl() { :; }; "
            "deploy_compose_stack; [[ $drain_calls -eq 1 ]]"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_systemd_restart_failure_never_enters_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = self.source_and_run(
                "systemctl() { "
                "  if [[ \"$*\" == *'restart'* ]]; then return 7; fi; return 0; "
                "}; validate_user_systemd_unit_contract() { MONOMER_SYSTEMD_UNIT_AVAILABLE=true; }; "
                "install_systemd_worker_env_helper() { :; }; stop_monomer_worker() { :; }; "
                "restart_monomer_worker_with_user_systemd; echo UNSAFE_FALLBACK",
                XDG_RUNTIME_DIR=temporary,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("refusing pidfile fallback", completed.stderr)
        self.assertNotIn("UNSAFE_FALLBACK", completed.stdout)

    def test_systemd_dropins_and_loaded_uninstalled_units_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            drop_in = self.source_and_run(
                "systemctl() { case \"$*\" in "
                "*list-unit-files*) printf '%s enabled\\n' \"$MONOMER_MD_WORKER_SYSTEMD_UNIT\" ;; "
                "*LoadState*) printf 'loaded\\n' ;; *ActiveState*) printf 'active\\n' ;; "
                "*FragmentPath*) printf '/tmp/unit\\n' ;; "
                "*DropInPaths*) printf '/tmp/unsafe.conf\\n' ;; esac; }; "
                "validate_user_systemd_unit_contract",
                XDG_RUNTIME_DIR=temporary,
            )
            loaded_without_file = self.source_and_run(
                "systemctl() { case \"$*\" in "
                "*list-unit-files*) : ;; *LoadState*) printf 'loaded\\n' ;; "
                "*ActiveState*) printf 'active\\n' ;; esac; }; "
                "validate_user_systemd_unit_contract",
                XDG_RUNTIME_DIR=temporary,
            )
        self.assertNotEqual(drop_in.returncode, 0)
        self.assertIn("drop-ins", drop_in.stderr)
        self.assertNotEqual(loaded_without_file.returncode, 0)
        self.assertIn("refusing fallback", loaded_without_file.stderr)

    def test_worker_readiness_wait_uses_one_absolute_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args_file = Path(temporary) / "curl.args"
            completed = self.source_and_run(
                f"curl() {{ printf '%s\\n' \"$*\" > \"{args_file}\"; return 1; }}; "
                "probe_monomer_worker_payload() { return 1; }; "
                "sleep() { SECONDS=$((SECONDS + 60)); }; "
                "MONOMER_MD_HEALTH_PROBE_TIMEOUT_SECONDS=1; "
                "MONOMER_MD_WORKER_UDS=; MONOMER_MD_WORKER_HOST=127.0.0.1; "
                "wait_for_monomer_worker"
            )
            curl_args = args_file.read_text(encoding="utf-8")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--max-time 6", curl_args)
        self.assertNotIn("seq 1 90", DEPLOY_SCRIPT.read_text(encoding="utf-8"))

    def test_fallback_child_does_not_inherit_deployment_flock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_file = Path(temporary) / "deploy.lock"
            completed = self.source_and_run(
                f'DEPLOY_LOCK_FILE="{lock_file}"; acquire_deploy_lock; '
                "( exec 9>&-; sleep 5 ) & child=$!; "
                "exec 9>&-; flock -n \"$DEPLOY_LOCK_FILE\" -c true; "
                "status=$?; kill $child 2>/dev/null || true; wait $child 2>/dev/null || true; "
                "exit $status"
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        fallback = script.split("local capture_summary=\"\"", 1)[1].split(
            "wait_for_monomer_worker", 1
        )[0]
        self.assertLess(fallback.index("exec 9>&-"), fallback.index("setsid"))

    def test_systemd_and_run_host_use_only_the_literal_helper_contract(self) -> None:
        unit = SYSTEMD_UNIT.read_text(encoding="utf-8")
        launcher = RUN_HOST_WORKER.read_text(encoding="utf-8")
        helper = ENV_HELPER.read_text(encoding="utf-8")
        self.assertNotIn("EnvironmentFile=", "\n".join(
            line for line in unit.splitlines() if not line.lstrip().startswith("#")
        ))
        self.assertIn("monomer_worker_env.py exec", unit)
        self.assertIn("--nexpoly-worker-env-applied", unit)
        self.assertIn('"${1:-}" != "--nexpoly-worker-env-applied"', launcher)
        self.assertNotIn('export PATH="$(dirname', launcher)
        self.assertIn('key.startswith("BASH_FUNC_")', helper)
        self.assertIn('"LD_AUDIT"', helper)

    def test_worker_pid_exemption_rejects_changed_process_identity(self) -> None:
        completed = self.source_and_run(
            "PREVIOUS_WORKER_PID=$$; "
            "capture_worker_pid_identity \"$PREVIOUS_WORKER_PID\"; "
            "worker_pid_identity_matches; "
            "PREVIOUS_WORKER_PID_START_TICKS=$((PREVIOUS_WORKER_PID_START_TICKS + 1)); "
            "if worker_pid_identity_matches; then exit 9; fi",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_stale_pidfile_pointing_to_unrelated_process_is_never_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unrelated = subprocess.Popen(["sleep", "30"], cwd=REPO_ROOT)
            try:
                pidfile = root / "worker.pid"
                pidfile.write_text(f"{unrelated.pid}\n", encoding="utf-8")
                completed = self.source_and_run(
                    "MONOMER_MD_WORKER_DEPLOY_MODE=true; "
                    "capture_monomer_worker_state; "
                    "[[ \"$PREVIOUS_WORKER_ACTIVE\" == unknown ]]; "
                    "[[ -z \"$PREVIOUS_WORKER_PID_START_TICKS\" ]]",
                    NEXPOLY_MONOMER_MD_WORKER_PID_FILE=str(pidfile),
                    XDG_RUNTIME_DIR=str(root / "missing-systemd-runtime"),
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)

    def test_candidate_venv_activation_can_restore_previous_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = root / "venvs" / "previous"
            candidate = root / "venvs" / ("b" * 40)
            previous.mkdir(parents=True)
            candidate.mkdir()
            (root / "current").symlink_to(previous)
            completed = self.source_and_run(
                f'MONOMER_MD_VENV_ROOT="{root}"; '
                "MONOMER_MD_WORKER_DEPLOY_MODE=true; "
                f'CANDIDATE_WORKER_VENV="{candidate}"; '
                f'PREVIOUS_WORKER_VENV_TARGET="{previous}"; '
                f'TARGET_COMMIT="{"b" * 40}"; '
                "activate_candidate_worker_venv; "
                f'[[ "$(readlink -f "{root}/current")" == "{candidate}" ]]; '
                "restore_previous_worker_venv_after_failed_restart; "
                f'[[ "$(readlink -f "{root}/current")" == "{previous}" ]]',
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_current_pointer_outside_versioned_sha_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target_sha = "d" * 40
            candidate = root / "venvs" / target_sha
            external = root / "shared-conda-base"
            test_worktree = root / "target-source"
            requirements = test_worktree / "workers" / "monomer_md_worker" / "requirements.txt"
            requirements.parent.mkdir(parents=True)
            requirements.write_bytes(
                (REPO_ROOT / "workers" / "monomer_md_worker" / "requirements.txt").read_bytes()
            )
            external.mkdir(parents=True)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "venv",
                    "--system-site-packages",
                    str(candidate),
                ],
                check=True,
            )
            marker = candidate / ".nexpoly-worker-release.json"
            marker.write_text(
                json.dumps(
                    {
                        "base_python_realpath": str(Path(sys.executable).resolve()),
                        "release_sha": target_sha,
                        "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            marker.chmod(0o600)
            (root / "current").symlink_to(external)

            completed = self.source_and_run(
                f'MONOMER_MD_VENV_ROOT="{root}"; '
                "MONOMER_MD_WORKER_DEPLOY_MODE=true; "
                f'TARGET_COMMIT="{target_sha}"; '
                f'BYTEFF2_PYTHON="{Path(sys.executable).resolve()}"; '
                f'TEST_WORKTREE="{test_worktree}"; '
                "prepare_candidate_worker_venv",
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("versioned 40-hex release", completed.stderr)

    def test_candidate_marker_cannot_hide_a_wrong_base_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target_sha = "f" * 40
            candidate = root / "venvs" / target_sha
            test_worktree = root / "target-source"
            requirements = test_worktree / "workers" / "monomer_md_worker" / "requirements.txt"
            requirements.parent.mkdir(parents=True)
            requirements.write_bytes(
                (REPO_ROOT / "workers" / "monomer_md_worker" / "requirements.txt").read_bytes()
            )
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "venv",
                    "--system-site-packages",
                    str(candidate),
                ],
                check=True,
            )
            fake_base = root / "wrong-base-python"
            fake_base.write_text(
                "#!/usr/bin/env bash\n"
                "printf '/definitely/wrong/base-prefix\\n'\n",
                encoding="utf-8",
            )
            fake_base.chmod(0o700)
            marker = candidate / ".nexpoly-worker-release.json"
            marker.write_text(
                json.dumps(
                    {
                        "base_python_realpath": str(fake_base.resolve()),
                        "release_sha": target_sha,
                        "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            marker.chmod(0o600)

            completed = self.source_and_run(
                f'MONOMER_MD_VENV_ROOT="{root}"; '
                "MONOMER_MD_WORKER_DEPLOY_MODE=true; "
                f'TARGET_COMMIT="{target_sha}"; '
                f'BYTEFF2_PYTHON="{fake_base}"; '
                f'TEST_WORKTREE="{test_worktree}"; '
                "prepare_candidate_worker_venv",
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("invalid or lacks required packages", completed.stderr)


class WorkerVenvProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.release_id = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.fake_python = self.root / "fake-python"
        self.fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s|PYTHONPATH=%s|PIP_TARGET=%s|PIP_PREFIX=%s|PIP_USER=%s|PIP_CONFIG_FILE=%s\\n' \"$*\" \"${PYTHONPATH-unset}\" \"${PIP_TARGET-unset}\" \"${PIP_PREFIX-unset}\" \"${PIP_USER-unset}\" \"${PIP_CONFIG_FILE-unset}\" >> \"$FAKE_PYTHON_LOG\"\n"
            "if [[ \"$1 $2\" == '-I -c' && \"$3\" == *'print(os.path.realpath(sys.prefix))'* ]]; then\n"
            "  printf '%s\\n' \"$FAKE_BASE_PREFIX\"\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1 $2 $3\" == '-I -m venv' ]]; then\n"
            "  target=\"${!#}\"\n"
            "  mkdir -p \"$target/bin\"\n"
            "  cp \"$0\" \"$target/bin/python\"\n"
            "  chmod 700 \"$target/bin/python\"\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        self.fake_python.chmod(0o700)
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "BYTEFF2_PYTHON": str(self.fake_python),
                "FAKE_BASE_PREFIX": str(self.root / "fake-base-prefix"),
                "FAKE_PYTHON_LOG": str(self.root / "fake-python.log"),
                "NEXPOLY_MONOMER_MD_VENV_ROOT": str(self.runtime),
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_provision(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(PROVISION_SCRIPT), *args],
            cwd=REPO_ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_default_provisions_without_activation_and_activate_is_explicit(self) -> None:
        provision = self.run_provision("--release-id", self.release_id)
        candidate = self.runtime / "venvs" / self.release_id
        marker = candidate / ".nexpoly-worker-release.json"

        self.assertEqual(provision.returncode, 0, provision.stderr)
        self.assertTrue((candidate / "bin" / "python").exists())
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(marker.read_text())["release_sha"], self.release_id)
        self.assertFalse((self.runtime / "current").exists())

        activate = self.run_provision(
            "--release-id", self.release_id, "--activate"
        )
        self.assertEqual(activate.returncode, 0, activate.stderr)
        self.assertEqual((self.runtime / "current").resolve(), candidate)

    def test_wrong_checkout_sha_fails_before_creating_candidate(self) -> None:
        wrong_sha = "e" * 40
        completed = self.run_provision("--release-id", wrong_sha)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("HEAD does not match", completed.stderr)
        self.assertFalse((self.runtime / "venvs" / wrong_sha).exists())

    def test_partial_existing_candidate_is_rejected_without_nested_move(self) -> None:
        candidate = self.runtime / "venvs" / self.release_id
        candidate.mkdir(parents=True)
        completed = self.run_provision("--release-id", self.release_id)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("incomplete", completed.stderr)
        self.assertEqual(list(candidate.iterdir()), [])

    def test_concurrent_provisioners_serialize_on_the_same_release(self) -> None:
        command = [
            "bash",
            str(PROVISION_SCRIPT),
            "--release-id",
            self.release_id,
        ]
        first = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        first_stdout, first_stderr = first.communicate(timeout=20)
        second_stdout, second_stderr = second.communicate(timeout=20)
        self.assertEqual(first.returncode, 0, first_stderr + first_stdout)
        self.assertEqual(second.returncode, 0, second_stderr + second_stdout)
        self.assertTrue(
            (self.runtime / "venvs" / self.release_id / ".nexpoly-worker-release.json").is_file()
        )
        self.assertEqual(
            list((self.runtime / "venvs").glob(f".{self.release_id}.tmp.*")), []
        )

    def test_pip_is_isolated_and_frozen_base_file_is_unchanged(self) -> None:
        frozen_sentinel = self.root / "frozen-base-sentinel"
        frozen_sentinel.write_bytes(b"frozen\n")
        before_hash = hashlib.sha256(frozen_sentinel.read_bytes()).hexdigest()
        before_mtime = frozen_sentinel.stat().st_mtime_ns
        self.environment.update(
            {
                "PYTHONPATH": str(self.root / "injected-pythonpath"),
                "PIP_TARGET": str(self.root / "forbidden-target"),
                "PIP_PREFIX": str(self.root / "forbidden-prefix"),
                "PIP_USER": "1",
            }
        )

        completed = self.run_provision("--release-id", self.release_id)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        pip_lines = [
            line
            for line in (self.root / "fake-python.log").read_text().splitlines()
            if "-I -m pip" in line
        ]
        self.assertEqual(len(pip_lines), 1)
        self.assertIn("--isolated --require-virtualenv", pip_lines[0])
        self.assertIn("PYTHONPATH=unset", pip_lines[0])
        self.assertIn("PIP_TARGET=unset", pip_lines[0])
        self.assertIn("PIP_PREFIX=unset", pip_lines[0])
        self.assertIn("PIP_USER=unset", pip_lines[0])
        self.assertIn("PIP_CONFIG_FILE=/dev/null", pip_lines[0])
        self.assertEqual(
            hashlib.sha256(frozen_sentinel.read_bytes()).hexdigest(), before_hash
        )
        self.assertEqual(frozen_sentinel.stat().st_mtime_ns, before_mtime)
        self.assertFalse((self.root / "forbidden-target").exists())


class DeploymentWorkflowIsolationTests(unittest.TestCase):
    def test_non_main_deploys_use_the_workflow_tested_commit_sha(self) -> None:
        workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        bundle_step = workflow.split("- name: Create deployment bundle", 1)[1].split(
            "- name: Upload deployment bundle", 1
        )[0]
        run_step = workflow.split("- name: Run server deployment", 1)[1]
        bootstrap = run_step.split("<<'REMOTE_BOOTSTRAP'", 1)[1].split(
            "REMOTE_BOOTSTRAP", 1
        )[0]

        self.assertIn("target_sha=\"$(git rev-parse 'HEAD^{commit}')\"", bundle_step)
        self.assertIn('remote_deploy_ref="$target_sha"', bundle_step)
        self.assertIn(
            'NEXPOLY_REMOTE_DEPLOY_REF=$remote_deploy_ref', bundle_step
        )
        self.assertIn(
            'remote_ref="$(printf \'%q\' "$NEXPOLY_REMOTE_DEPLOY_REF")"',
            run_step,
        )
        self.assertNotIn('remote_ref="$(printf \'%q\' "$DEPLOY_REF")"', run_step)
        self.assertNotIn('"origin/${NEXPOLY_DEPLOY_REF}^{commit}"', bootstrap)
        self.assertIn(
            '[[ "$NEXPOLY_DEPLOY_REF" =~ ^[0-9a-f]{40}$ ]]', bootstrap
        )
        self.assertIn(
            '[[ "$target_commit" != "$NEXPOLY_EXPECTED_TARGET_SHA" ]]',
            bootstrap,
        )

    def test_remote_bootstrap_uses_driver_worktree_without_switching_production(self) -> None:
        workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        bootstrap = workflow.split("<<'REMOTE_BOOTSTRAP'", 1)[1].split(
            "REMOTE_BOOTSTRAP", 1
        )[0]
        invocation = bootstrap.index('bash "$DRIVER_WORKTREE/scripts/deploy_server.sh"')
        before_invocation = bootstrap[:invocation]

        self.assertNotRegex(before_invocation, r"\bgit\s+(?:checkout|merge)\b")
        self.assertIn('git worktree add --detach "$DRIVER_WORKTREE"', before_invocation)
        self.assertIn('NEXPOLY_DEPLOY_ROOT="$PWD"', bootstrap)
        self.assertIn("NEXPOLY_DEPLOY_DRIVER_CONTRACT=1", bootstrap)
        provisioning = bootstrap.index(
            'provision_monomer_md_worker_venv.sh"'
        )
        self.assertLess(provisioning, invocation)
        self.assertIn('--release-id "$target_commit"', bootstrap)

    def test_isolated_driver_records_old_production_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            production = Path(temporary) / "production"
            production.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=production, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=production,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=production, check=True
            )
            tracked = production / "tracked.txt"
            tracked.write_text("old\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=production, check=True)
            subprocess.run(["git", "commit", "-qm", "old"], cwd=production, check=True)
            old_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=production,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            tracked.write_text("target\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "target"], cwd=production, check=True)
            target_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=production,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "checkout", "-q", "--detach", old_sha],
                cwd=production,
                check=True,
            )

            completed = subprocess.run(
                [
                    "bash",
                    "-c",
                    (
                        f'source "{DEPLOY_SCRIPT}"; '
                        "docker() { return 0; }; "
                        f'TARGET_COMMIT="{target_sha}"; '
                        "initialize_deploy_state"
                    ),
                ],
                cwd=REPO_ROOT,
                env={
                    **os.environ,
                    "NEXPOLY_DEPLOY_ROOT": str(production),
                    "NEXPOLY_DEPLOY_DRIVER_CONTRACT": "1",
                    "NEXPOLY_MONOMER_MD_WORKER_MODE": "false",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            records = list(
                (production / "ops" / "state").glob(
                    f"deploy-{target_sha}-*.json"
                )
            )
            self.assertEqual(len(records), 1)
            state = json.loads(records[0].read_text())
            current_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=production,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(state["previous_sha"], old_sha)
            self.assertEqual(state["target_sha"], target_sha)
            self.assertEqual(current_sha, old_sha)

    def test_repeated_target_sha_keeps_distinct_deploy_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            production = Path(temporary) / "production"
            production.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=production, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=production,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=production,
                check=True,
            )
            (production / "tracked").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked"], cwd=production, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=production, check=True)
            target_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=production,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            completed = subprocess.run(
                [
                    "bash",
                    "-c",
                    (
                        f'source "{DEPLOY_SCRIPT}"; docker() {{ return 0; }}; '
                        f'TARGET_COMMIT="{target_sha}"; '
                        "initialize_deploy_state; first=$DEPLOY_STATE_FILE; "
                        "initialize_deploy_state; [[ -f $first && -f $DEPLOY_STATE_FILE && $first != $DEPLOY_STATE_FILE ]]"
                    ),
                ],
                cwd=REPO_ROOT,
                env={
                    **os.environ,
                    "NEXPOLY_DEPLOY_ROOT": str(production),
                    "NEXPOLY_DEPLOY_DRIVER_CONTRACT": "1",
                    "NEXPOLY_MONOMER_MD_WORKER_MODE": "false",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                len(
                    list(
                        (production / "ops" / "state").glob(
                            f"deploy-{target_sha}-*.json"
                        )
                    )
                ),
                2,
            )


class DeployStateTests(unittest.TestCase):
    def test_state_is_owner_only_and_contains_no_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "deploy.json"
            subprocess.run(
                [
                    sys.executable,
                    str(STATE_HELPER),
                    "init",
                    str(path),
                    "--previous-sha",
                    "a" * 40,
                    "--target-sha",
                    "b" * 40,
                    "--service",
                    "backend=container=image",
                    "--worker-unit",
                    "nexpoly-monomer-md-worker.service",
                    "--worker-pid",
                    "123",
                    "--worker-active",
                    "true",
                    "--previous-venv-target",
                    "/runtime/venvs/old",
                    "--candidate-venv-target",
                    "/runtime/venvs/new",
                ],
                check=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(STATE_HELPER),
                    "update",
                    str(path),
                    "--phase",
                    "worker_restart",
                    "--status",
                    "failed",
                    "--error-category",
                    "deploy_command_failed",
                ],
                check=True,
            )

            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["worker"]["pid"], 123)
            self.assertEqual(state["venv"]["previous_target"], "/runtime/venvs/old")
            self.assertEqual(state["venv"]["candidate_target"], "/runtime/venvs/new")
            self.assertNotIn("environment", state)

    def test_state_update_rejects_symlinks_and_non_owner_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real.json"
            real.write_text("{}\n", encoding="utf-8")
            real.chmod(0o600)
            linked = root / "linked.json"
            linked.symlink_to(real)
            symlink_update = subprocess.run(
                [
                    sys.executable,
                    str(STATE_HELPER),
                    "update",
                    str(linked),
                    "--phase",
                    "failed",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            real.chmod(0o644)
            mode_update = subprocess.run(
                [
                    sys.executable,
                    str(STATE_HELPER),
                    "update",
                    str(real),
                    "--phase",
                    "failed",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(symlink_update.returncode, 0)
        self.assertNotEqual(mode_update.returncode, 0)


if __name__ == "__main__":
    unittest.main()

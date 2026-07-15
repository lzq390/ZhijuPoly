from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class GpuResourceDeliveryTests(unittest.TestCase):
    def _inventory_text(self, blocked: dict[str, str] | None = None) -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "blocked_gpu_uuids": blocked or {},
                "managed_docker_claims": {},
                "managed_systemd_claims": {},
            },
            sort_keys=True,
        ) + "\n"

    def _mps_environment(self, temporary_root: Path) -> dict[str, str]:
        binary_root = temporary_root / "bin"
        binary_root.mkdir()
        nvidia_smi = binary_root / "nvidia-smi"
        nvidia_smi.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *--query-gpu=uuid,compute_mode*) printf '%s\\n' 'GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771, Exclusive_Process' ;;\n"
            "  *--query-gpu=uuid*) printf '%s\\n' 'GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771' ;;\n"
            "  *--query-compute-apps=*) exit 0 ;;\n"
            "  *) exit 98 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        nvidia_smi.chmod(0o755)
        mps_control = binary_root / "nvidia-cuda-mps-control"
        mps_control.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = '-d' ]; then exit 0; fi\n"
            "request=$(cat)\n"
            "if [ \"$request\" = 'ps' ]; then\n"
            "  printf '%s\\n' 'PID ID SERVER DEVICE NAMESPACE COMMAND'\n"
            "fi\n",
            encoding="utf-8",
        )
        mps_control.chmod(0o755)
        docker = binary_root / "docker"
        docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        docker.chmod(0o755)
        systemctl = binary_root / "systemctl"
        systemctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        systemctl.chmod(0o755)
        return {
            **os.environ,
            "PATH": f"{binary_root}:{os.environ['PATH']}",
            "NEXPOLY_GPU_STATE_ROOT": str(temporary_root / "state"),
            "NEXPOLY_GPU_EXTERNAL_RESERVATIONS": str(
                temporary_root / "external.json"
            ),
        }

    def _run_mps(
        self,
        action: str,
        environment: dict[str, str],
        *,
        index: int = 1,
        extra_arguments: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(ROOT / "scripts/gpu_mps_control.sh"),
                action,
                str(index),
                *extra_arguments,
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_fixed_policy_matches_4090_budget_and_uuid_contract(self) -> None:
        policy = json.loads(
            (ROOT / "ops/config/gpu-broker-policy.json").read_text(encoding="utf-8")
        )
        self.assertEqual(policy["gpu_total_budget_mib"], 20736)
        self.assertEqual(
            policy["component_budgets_mib"],
            {"backend": 8192, "dft": 4096, "md": 8192},
        )
        self.assertEqual(
            policy["component_thread_percent"],
            {"backend": 100, "dft": 50, "md": 50},
        )
        self.assertNotIn("0", policy["gpu_uuids"])
        self.assertEqual(policy["device_policy"]["prod.md"], [2, 3, 1])
        self.assertEqual(policy["device_policy"]["dev.md"], [1, 3])

    def test_images_run_as_shared_nonroot_identity(self) -> None:
        for relative in ("Dockerfile", "workers/monomer_md_worker/Dockerfile"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("USER 1001:1001", source)
            self.assertIn("COPY gpu_resource", source)

    def test_production_broker_and_worker_are_explicitly_default_off(self) -> None:
        production = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
        worker = (ROOT / "ops/config/worker.env.example").read_text(encoding="utf-8")
        self.assertIn('GPU_BROKER_ENABLED: "false"', production)
        self.assertIn("MONOMER_MD_GPU_BROKER_ENABLED=false", worker)
        self.assertNotIn("gpu-broker/broker.sock:/app/gpu-broker", production)

    def test_opt_in_gpu_compose_exposes_only_governed_devices_and_control_paths(self) -> None:
        backend = (ROOT / "docker-compose.gpu-governed.yml").read_text(
            encoding="utf-8"
        )
        md_prod = (
            ROOT / "docker-compose.monomer-md-worker.gpu-governed.prod.yml"
        ).read_text(encoding="utf-8")
        md_dev = (
            ROOT / "docker-compose.monomer-md-worker.gpu-governed.dev.yml"
        ).read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        for source in (backend, md_prod, md_dev):
            self.assertIn('user: "1001:1001"', source)
            self.assertIn("/app/gpu-mps/broker.sock", source)
            self.assertIn("com.nexpoly.gpu.registration", source)
        self.assertIn('device_ids: ["1", "2", "3"]', md_prod)
        self.assertIn('device_ids: ["1", "3"]', md_dev)
        self.assertNotIn('"0"', md_prod + md_dev)
        self.assertIn("docker-compose.gpu-governed.yml config --quiet", workflow)
        self.assertIn(
            "docker-compose.monomer-md-worker.gpu-governed.prod.yml config --quiet",
            workflow,
        )
        self.assertIn(
            "docker-compose.monomer-md-worker.gpu-governed.dev.yml config --quiet",
            workflow,
        )

    def test_mps_templates_never_change_compute_mode_and_are_not_enabled(self) -> None:
        unit = (ROOT / "ops/systemd/nexpoly-gpu-mps@.service").read_text(encoding="utf-8")
        helper = (ROOT / "scripts/gpu_mps_control.sh").read_text(encoding="utf-8")
        broker_unit = (ROOT / "ops/systemd/nexpoly-gpu-broker.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("NoNewPrivileges=true", broker_unit)
        self.assertIn("User=1001", unit)
        self.assertIn("Group=1001", unit)
        self.assertIn("User=1001", broker_unit)
        self.assertIn("Group=1001", broker_unit)
        self.assertIn("install -m 0600", unit)
        self.assertIn("install -m 0600", broker_unit)
        self.assertNotIn("systemctl enable", unit + broker_unit + helper)
        self.assertNotIn("nvidia-smi -c", helper)
        self.assertIn("GPU0 is excluded", helper)
        self.assertIn("--mps-state-root", broker_unit)
        self.assertNotIn("--break-glass-without-broker", unit)
        self.assertNotIn("NEXPOLY_GPU_MPS_BREAK_GLASS_REASON", unit)
        self.assertTrue(os.access(ROOT / "scripts/gpu_mps_control.sh", os.X_OK))

    @unittest.skipUnless(
        os.getuid() == 1001 and os.getgid() == 1001,
        "MPS helper intentionally requires the production 1001:1001 identity",
    )
    def test_mps_start_accepts_only_private_valid_unblocked_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            environment = self._mps_environment(temporary_root)
            inventory = Path(environment["NEXPOLY_GPU_EXTERNAL_RESERVATIONS"])
            inventory.write_text(
                self._inventory_text(),
                encoding="utf-8",
            )
            inventory.chmod(0o600)

            completed = self._run_mps("start", environment)

            self.assertEqual(completed.returncode, 0, completed.stderr)

    @unittest.skipUnless(
        os.getuid() == 1001 and os.getgid() == 1001,
        "MPS helper intentionally requires the production 1001:1001 identity",
    )
    def test_mps_start_requires_exclusive_mode_and_zero_cuda_clients(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            environment = self._mps_environment(temporary_root)
            inventory = Path(environment["NEXPOLY_GPU_EXTERNAL_RESERVATIONS"])
            inventory.write_text(
                self._inventory_text(),
                encoding="utf-8",
            )
            inventory.chmod(0o600)
            nvidia_smi = Path(environment["PATH"].split(":", 1)[0]) / "nvidia-smi"
            nvidia_smi.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *--query-gpu=uuid,compute_mode*) printf '%s\\n' 'GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771, Default' ;;\n"
                "  *--query-compute-apps=*) exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            nvidia_smi.chmod(0o755)
            self.assertNotEqual(self._run_mps("start", environment).returncode, 0)

            nvidia_smi.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *--query-gpu=uuid,compute_mode*) printf '%s\\n' 'GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771, Exclusive_Process' ;;\n"
                "  *--query-compute-apps=*) printf '%s\\n' 'GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771, 4242' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            nvidia_smi.chmod(0o755)
            self.assertNotEqual(self._run_mps("start", environment).returncode, 0)

    @unittest.skipUnless(
        os.getuid() == 1001 and os.getgid() == 1001,
        "MPS helper intentionally requires the production 1001:1001 identity",
    )
    def test_mps_start_accepts_real_exclusive_process_spellings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            environment = self._mps_environment(temporary_root)
            inventory = Path(environment["NEXPOLY_GPU_EXTERNAL_RESERVATIONS"])
            inventory.write_text(self._inventory_text(), encoding="utf-8")
            inventory.chmod(0o600)
            nvidia_smi = Path(environment["PATH"].split(":", 1)[0]) / "nvidia-smi"

            for compute_mode in ("Exclusive Process", "EXCLUSIVE_PROCESS"):
                with self.subTest(compute_mode=compute_mode):
                    nvidia_smi.write_text(
                        "#!/bin/sh\n"
                        "case \"$*\" in\n"
                        "  *--query-gpu=uuid,compute_mode*) "
                        f"printf '%s\\n' 'GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771, {compute_mode}' ;;\n"
                        "  *--query-compute-apps=*) exit 0 ;;\n"
                        "  *) exit 98 ;;\n"
                        "esac\n",
                        encoding="utf-8",
                    )
                    nvidia_smi.chmod(0o755)
                    completed = self._run_mps("start", environment)
                    self.assertEqual(completed.returncode, 0, completed.stderr)

    @unittest.skipUnless(
        os.getuid() == 1001 and os.getgid() == 1001,
        "MPS helper intentionally requires the production 1001:1001 identity",
    )
    def test_mps_start_rejects_device_request_without_cuda_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            environment = self._mps_environment(temporary_root)
            inventory = Path(environment["NEXPOLY_GPU_EXTERNAL_RESERVATIONS"])
            inventory.write_text(self._inventory_text(), encoding="utf-8")
            inventory.chmod(0o600)
            binary_root = Path(environment["PATH"].split(":", 1)[0])
            container_id = "d" * 64
            inspect_payload = json.dumps(
                [
                    {
                        "Id": container_id,
                        "State": {"Running": True, "Pid": os.getpid()},
                        "Config": {"Labels": {}, "Env": []},
                        "HostConfig": {
                            "DeviceRequests": [
                                {
                                    "Driver": "nvidia",
                                    "DeviceIDs": ["1"],
                                    "Capabilities": [["gpu"]],
                                    "Count": 0,
                                }
                            ]
                        },
                    }
                ]
            )
            docker = binary_root / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                "if [ \"$2\" = 'ls' ]; then\n"
                f"  printf '%s\\n' '{container_id}'\n"
                "else\n"
                f"  printf '%s\\n' '{inspect_payload}'\n"
                "fi\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)

            completed = self._run_mps("start", environment)

            self.assertNotEqual(completed.returncode, 0)

    @unittest.skipUnless(
        os.getuid() == 1001 and os.getgid() == 1001,
        "MPS helper intentionally requires the production 1001:1001 identity",
    )
    def test_mps_start_fails_closed_for_missing_unsafe_or_invalid_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            environment = self._mps_environment(temporary_root)
            inventory = Path(environment["NEXPOLY_GPU_EXTERNAL_RESERVATIONS"])
            valid_target = temporary_root / "valid-target.json"
            valid_target.write_text(
                self._inventory_text(),
                encoding="utf-8",
            )
            valid_target.chmod(0o600)
            cases = {
                "missing": None,
                "world-readable": (
                    self._inventory_text(),
                    0o644,
                ),
                "malformed": ("not-json\n", 0o600),
                "unknown-uuid": (
                    self._inventory_text({"GPU-unknown": "claim"}),
                    0o600,
                ),
                "blank-reason": (
                    self._inventory_text(
                        {"GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5": " "}
                    ),
                    0o600,
                ),
                "selected-blocked": (
                    self._inventory_text(
                        {"GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771": "claim"}
                    ),
                    0o600,
                ),
            }
            for name, specification in cases.items():
                with self.subTest(name=name):
                    inventory.unlink(missing_ok=True)
                    if specification is not None:
                        content, mode = specification
                        inventory.write_text(content, encoding="utf-8")
                        inventory.chmod(mode)
                    completed = self._run_mps("start", environment)
                    self.assertNotEqual(completed.returncode, 0)

            inventory.unlink(missing_ok=True)
            inventory.symlink_to(valid_target)
            completed = self._run_mps("start", environment)
            self.assertNotEqual(completed.returncode, 0)

    @unittest.skipUnless(
        os.getuid() == 1001 and os.getgid() == 1001,
        "MPS helper intentionally requires the production 1001:1001 identity",
    )
    def test_mps_stop_requires_exact_gpu_empty_clients_and_safe_control(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            environment = self._mps_environment(temporary_root)
            control = (
                Path(environment["NEXPOLY_GPU_STATE_ROOT"])
                / "mps-1"
                / "pipe"
                / "control"
            )
            control.parent.mkdir(parents=True)
            os.mkfifo(control, 0o600)

            completed = self._run_mps("stop", environment)

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Broker socket is missing", completed.stderr)

            environment["NEXPOLY_GPU_MPS_BREAK_GLASS_REASON"] = (
                "incident INC-1234: Broker host process unavailable"
            )
            completed = self._run_mps(
                "stop",
                environment,
                extra_arguments=("--break-glass-without-broker",),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("SECURITY AUDIT", completed.stderr)
            audit_path = (
                Path(environment["NEXPOLY_GPU_STATE_ROOT"])
                / "mps-break-glass-audit.jsonl"
            )
            audit_records = [
                json.loads(line)
                for line in audit_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(audit_records), 1)
            self.assertEqual(
                audit_records[0]["event"], "mps_stop_without_broker_authorized"
            )
            self.assertEqual(
                audit_records[0]["reason"],
                environment["NEXPOLY_GPU_MPS_BREAK_GLASS_REASON"],
            )
            self.assertEqual(audit_path.stat().st_mode & 0o777, 0o600)

            nvidia_smi = Path(environment["PATH"].split(":", 1)[0]) / "nvidia-smi"
            nvidia_smi.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            nvidia_smi.chmod(0o755)
            self.assertNotEqual(self._run_mps("stop", environment).returncode, 0)

    @unittest.skipUnless(
        os.getuid() == 1001 and os.getgid() == 1001,
        "MPS helper intentionally requires the production 1001:1001 identity",
    )
    def test_mps_stop_break_glass_requires_flag_reason_and_missing_broker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            environment = self._mps_environment(temporary_root)
            control = (
                Path(environment["NEXPOLY_GPU_STATE_ROOT"])
                / "mps-1"
                / "pipe"
                / "control"
            )
            control.parent.mkdir(parents=True)
            os.mkfifo(control, 0o600)

            no_reason = self._run_mps(
                "stop",
                environment,
                extra_arguments=("--break-glass-without-broker",),
            )
            self.assertNotEqual(no_reason.returncode, 0)
            self.assertIn("BREAK_GLASS_REASON", no_reason.stderr)

            environment["NEXPOLY_GPU_MPS_BREAK_GLASS_REASON"] = "INC-5678"
            broker_path = Path(environment["NEXPOLY_GPU_STATE_ROOT"]) / "broker.sock"
            broker_path.write_text("not a socket\n", encoding="utf-8")
            unsafe_path = self._run_mps(
                "stop",
                environment,
                extra_arguments=("--break-glass-without-broker",),
            )
            self.assertNotEqual(unsafe_path.returncode, 0)
            self.assertNotIn("SECURITY AUDIT", unsafe_path.stderr)
            self.assertFalse(
                (
                    Path(environment["NEXPOLY_GPU_STATE_ROOT"])
                    / "mps-break-glass-audit.jsonl"
                ).exists()
            )

    @unittest.skipUnless(
        os.getuid() == 1001 and os.getgid() == 1001,
        "MPS helper intentionally requires the production 1001:1001 identity",
    )
    def test_mps_stop_refuses_any_live_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            environment = self._mps_environment(temporary_root)
            control = (
                Path(environment["NEXPOLY_GPU_STATE_ROOT"])
                / "mps-1"
                / "pipe"
                / "control"
            )
            control.parent.mkdir(parents=True)
            os.mkfifo(control, 0o600)
            mps_control = (
                Path(environment["PATH"].split(":", 1)[0])
                / "nvidia-cuda-mps-control"
            )
            mps_control.write_text(
                "#!/bin/sh\n"
                "request=$(cat)\n"
                "if [ \"$request\" = 'ps' ]; then\n"
                "  printf '%s\\n' 'PID ID SERVER DEVICE NAMESPACE COMMAND'\n"
                "  printf '%s\\n' '4242 1 5252 GPU-0e19c809-f81d 4026531836 client'\n"
                "fi\n",
                encoding="utf-8",
            )
            mps_control.chmod(0o755)

            completed = self._run_mps("stop", environment)

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("active clients", completed.stderr)


if __name__ == "__main__":
    unittest.main()

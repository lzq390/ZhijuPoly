from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import Any
import unittest

from scripts import bootstrap_asset_release, release_controller
from workers.monomer_md_worker.app.byteff2_runtime_assets import (
    BYTEFF2_RUNTIME_ASSETS,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPOSITORY_ROOT / "docker-compose.yml"
DEV_COMPOSE = REPOSITORY_ROOT / "docker-compose.dev.yml"
PROD_COMPOSE = REPOSITORY_ROOT / "docker-compose.prod.yml"
SYSTEMD_UNIT = REPOSITORY_ROOT / "ops/systemd/nexpoly-monomer-md-worker.service"
WORKER_ENV = REPOSITORY_ROOT / "ops/config/worker.env.example"
DEPLOY_ENV = REPOSITORY_ROOT / "ops/config/deploy.env.example"
WORKER_SCRIPT = REPOSITORY_ROOT / "workers/monomer_md_worker/run_host_worker.sh"
LEGACY_INSTALLER = REPOSITORY_ROOT / "scripts/install_monomer_md_worker_user_service.sh"
BACKEND_DIGEST = "ghcr.io/lzq390/nexpoly-backend@sha256:" + "a" * 64
WEB_DIGEST = "ghcr.io/lzq390/nexpoly-web@sha256:" + "b" * 64
PRODUCTION_SOCKET_TARGET = "/app/monomer-md-worker"
PRODUCTION_SOCKET = (
    "/data/lzq/gith/nexpoly-runtime/state/monomer-md-worker-socket/worker.sock"
)
FROZEN_BASE_BIN = "/home/devuser/miniconda3/envs/byteff2-repro/bin"
CURRENT_ASSET_BYTEFF2 = (
    "/data/lzq/gith/nexpoly-runtime/state/current-assets/byteff2"
)


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


@unittest.skipUnless(shutil.which("docker"), "Docker Compose is not available")
class ComposeWorkerRuntimeTests(unittest.TestCase):
    def _render(self, *compose_files: Path, environment: dict[str, str]) -> dict[str, Any]:
        command = ["docker", "compose", "-p", "nexpoly-test"]
        for compose_file in compose_files:
            command.extend(("-f", str(compose_file)))
        command.extend(("config", "--format", "json"))
        completed = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_production_uses_digest_images_loopback_postgres_and_stable_worker_uds(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app_env = root / "app.env"
            app_env.write_text("ONLINE_KNOWLEDGE_API_KEY=\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "NEXPOLY_BACKEND_IMAGE": BACKEND_DIGEST,
                    "NEXPOLY_WEB_IMAGE": WEB_DIGEST,
                    "NEXPOLY_POSTGRES_PASSWORD": "compose-test-only",
                    "APP_POSTGRES_DSN": "postgresql://nexpoly:test@lab-postgres:5432/nexpoly",
                    "PI_POSTGRES_DSN": "postgresql://nexpoly:test@lab-postgres:5432/nexpoly",
                    "LAB_DATA_POSTGRES_DSN": "postgresql://nexpoly:test@lab-postgres:5432/nexpoly",
                    "NEXPOLY_APP_ENV_FILE": str(app_env),
                    "NEXPOLY_ASSET_ROOT": str(root / "assets"),
                    "NEXPOLY_RUNTIME_ROOT": "/data/lzq/gith/nexpoly-runtime",
                    "POLYTAO_ENABLED": "true",
                }
            )
            document = self._render(BASE_COMPOSE, PROD_COMPOSE, environment=environment)

        services = document["services"]
        for service in ("postgres-init", "backend", "nginx"):
            self.assertNotIn("build", services[service])
        self.assertEqual(services["postgres-init"]["image"], BACKEND_DIGEST)
        self.assertEqual(services["backend"]["image"], BACKEND_DIGEST)
        self.assertEqual(services["nginx"]["image"], WEB_DIGEST)
        self.assertEqual(services["lab-postgres"]["ports"][0]["host_ip"], "127.0.0.1")

        init_targets = {mount["target"] for mount in services["postgres-init"].get("volumes", [])}
        self.assertNotIn(PRODUCTION_SOCKET_TARGET, init_targets)
        worker_mounts = [
            mount for mount in services["backend"]["volumes"]
            if mount["target"] == PRODUCTION_SOCKET_TARGET
        ]
        self.assertEqual(len(worker_mounts), 1)
        self.assertTrue(worker_mounts[0]["read_only"])
        self.assertEqual(
            services["backend"]["environment"]["MONOMER_MD_WORKER_BASE_URL"],
            "http+unix://%2Fapp%2Fmonomer-md-worker%2Fworker.sock",
        )

    def test_development_keeps_separate_socket_and_does_not_give_it_to_migrations(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            environment = os.environ.copy()
            environment["NEXPOLY_ASSET_ROOT"] = str(Path(raw) / "assets")
            document = self._render(BASE_COMPOSE, DEV_COMPOSE, environment=environment)

        services = document["services"]
        init_targets = {mount["target"] for mount in services["postgres-init"].get("volumes", [])}
        backend_targets = {mount["target"] for mount in services["backend"]["volumes"]}
        self.assertNotIn(PRODUCTION_SOCKET_TARGET, init_targets)
        self.assertIn(PRODUCTION_SOCKET_TARGET, backend_targets)
        self.assertEqual(
            services["backend"]["environment"]["MONOMER_MD_WORKER_BASE_URL"],
            "http+unix://%2Fapp%2Fmonomer-md-worker%2Fworker.sock",
        )


class WorkerHostRuntimeTests(unittest.TestCase):
    def test_byteff2_runtime_asset_contract_is_identical_across_delivery_layers(
        self,
    ) -> None:
        worker_contract = tuple(
            (asset.relative_path.as_posix(), asset.size, asset.sha256)
            for asset in BYTEFF2_RUNTIME_ASSETS
        )

        self.assertEqual(
            worker_contract,
            release_controller.BYTEFF2_FORMAL_RUNTIME_ASSETS,
        )
        self.assertEqual(
            tuple((path, digest) for path, _size, digest in worker_contract[:1]),
            bootstrap_asset_release.BYTEFF2_RUNTIME_REQUIRED_FILES,
        )
        self.assertEqual(
            worker_contract[1:],
            bootstrap_asset_release.BYTEFF2_AUDITED_OVERLAY_FILES,
        )
        self.assertEqual(
            release_controller.BYTEFF2_GIT_SOURCE,
            bootstrap_asset_release.BYTEFF2_GIT_SOURCE,
        )
        self.assertEqual(
            release_controller.BYTEFF2_GIT_REVISION,
            bootstrap_asset_release.BYTEFF2_GIT_REVISION,
        )

    def test_legacy_user_service_installer_is_a_fail_closed_shim(self) -> None:
        source = LEGACY_INSTALLER.read_text(encoding="utf-8")
        self.assertNotIn("systemctl --user", source)
        self.assertNotIn("loginctl ", source)
        self.assertNotIn("UNIT_SOURCE=", source)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_home = root / "home"
            fake_home.mkdir()
            completed = subprocess.run(
                [str(LEGACY_INSTALLER)],
                cwd=REPOSITORY_ROOT,
                env={**os.environ, "HOME": str(fake_home)},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(list(fake_home.iterdir()), [])

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("legacy installer is disabled and made no changes", completed.stderr)
        self.assertIn("docs/release-controller.md", completed.stderr)
        self.assertIn("scripts/bootstrap_pull_deploy.py", completed.stderr)
        self.assertIn("--production-root /data/lzq/gith/nexpoly", completed.stderr)
        self.assertIn("--runtime-root /data/lzq/gith/nexpoly-runtime", completed.stderr)
        self.assertNotIn("--source-root", completed.stderr)
        self.assertIn("docs/monomer-md-worker.md", completed.stderr)

    @unittest.skipUnless(shutil.which("flock"), "flock is not available")
    def test_systemd_marker_guard_allows_only_an_active_locked_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            marker = root / "deploy-in-progress.json"
            lock = root / "deploy.lock"
            command = [
                "/usr/bin/bash",
                "-c",
                '/usr/bin/test ! -e "$1" || ! /usr/bin/flock -n "$2" /usr/bin/true',
                "_",
                str(marker),
                str(lock),
            ]

            marker.touch()
            self.assertNotEqual(subprocess.run(command, check=False).returncode, 0)
            with lock.open("a+", encoding="utf-8") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(subprocess.run(command, check=False).returncode, 0)
            marker.unlink()
            self.assertEqual(subprocess.run(command, check=False).returncode, 0)

    def test_systemd_starts_active_ab_venv_through_stable_launcher(self) -> None:
        unit = SYSTEMD_UNIT.read_text(encoding="utf-8")
        self.assertIn(
            "WorkingDirectory=/data/lzq/gith/nexpoly",
            unit,
        )
        # The unit must not parse worker.env or duplicate runtime values.  Its
        # fixed, isolated stdlib helper validates the owner-only literal file,
        # scrubs inherited loader/Python variables, and then execs the venv.
        self.assertNotIn("EnvironmentFile=", unit)
        self.assertNotIn('Environment="PATH=', unit)
        self.assertNotIn('Environment="BYTEFF2_ROOT=', unit)
        self.assertNotIn('Environment="PYTHONPATH=', unit)
        self.assertIn(
            "ExecStart=/usr/bin/python3 -I -B "
            "/data/lzq/gith/nexpoly-runtime/bin/control_runtime_selector.py "
            "run monomer-md",
            unit,
        )
        self.assertIn("UnsetEnvironment=", unit)
        self.assertIn("/usr/bin/flock -n \"$2\" /usr/bin/true", unit)
        self.assertIn('/usr/bin/test ! -L "$1"', unit)
        self.assertIn('/usr/bin/test ! -L "$2"', unit)
        self.assertIn('$(/usr/bin/id -u):600', unit)
        self.assertIn(
            "/data/lzq/gith/nexpoly-runtime/state/deploy-in-progress.json ",
            unit,
        )
        self.assertIn("/data/lzq/gith/nexpoly-runtime/state/deploy.lock", unit)
        self.assertIn("/nexpoly-runtime/state/active-control.json", unit)
        self.assertNotIn("/nexpoly-runtime/bin/worker_slot_runtime.py", unit)
        self.assertNotIn("ops/current", unit)
        self.assertNotIn("ops/releases", unit)
        self.assertNotIn("worker-venv/bin/python", unit)
        self.assertIn("UMask=0077", unit)
        self.assertNotIn("pip install", unit)

    def test_production_templates_pin_release_and_asset_pointers(self) -> None:
        worker = _env_values(WORKER_ENV)
        deploy = _env_values(DEPLOY_ENV)
        self.assertEqual(worker["BYTEFF2_ROOT"], CURRENT_ASSET_BYTEFF2)
        self.assertEqual(worker["BYTEFF2_PYTHON"], f"{FROZEN_BASE_BIN}/python")
        self.assertEqual(
            worker["PYTHONPATH"],
            "/data/lzq/gith/nexpoly:"
            f"{CURRENT_ASSET_BYTEFF2}:{CURRENT_ASSET_BYTEFF2}/submodules/bytemol",
        )
        self.assertEqual(worker["MONOMER_MD_GPU_BROKER_ENABLED"], "false")
        self.assertEqual(worker["MONOMER_MD_GPU_BROKER_ENVIRONMENT"], "prod")
        self.assertEqual(
            worker["MONOMER_MD_GPU_MPS_PIPE_ROOT"],
            "/data/lzq/gith/nexpoly-runtime/state/gpu-resource",
        )
        self.assertNotIn("MONOMER_MD_PYTHON", worker)
        self.assertEqual(worker["MONOMER_MD_WORKER_UDS"], PRODUCTION_SOCKET)
        self.assertEqual(deploy["NEXPOLY_WORKER_BASE_PYTHON"], f"{FROZEN_BASE_BIN}/python")
        self.assertEqual(deploy["NEXPOLY_WORKER_GMX"], f"{FROZEN_BASE_BIN}/gmx")
        self.assertEqual(deploy["NEXPOLY_WORKER_CONDA_EXE"], "/home/devuser/miniconda3/bin/conda")
        self.assertEqual(deploy["POLYTAO_ENABLED"], "true")

    def test_host_launcher_derives_byteff2_paths_and_private_socket_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            byteff2_root = root / "assets/byteff2"
            base_bin = root / "base/bin"
            socket = root / "runtime/socket/worker.sock"
            capture = root / "capture"
            fake_python = root / "release-venv/bin/python"
            byteff2_root.mkdir(parents=True)
            base_bin.mkdir(parents=True)
            capture.mkdir()
            fake_python.parent.mkdir(parents=True)
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'printf "%s" "$PATH" >"$CAPTURE/path"\n'
                'printf "%s" "$PYTHONPATH" >"$CAPTURE/pythonpath"\n'
                'printf "%s" "$*" >"$CAPTURE/args"\n'
                ': >"$CAPTURE/umask-probe"\n'
                'stat -c "%a" "$(dirname "$MONOMER_MD_WORKER_UDS")" >"$CAPTURE/socket-dir-mode"\n',
                encoding="utf-8",
            )
            fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment.update(
                {
                    "APP_POSTGRES_DSN": "postgresql://worker:test@127.0.0.1:55432/nexpoly",
                    "BYTEFF2_ROOT": str(byteff2_root),
                    "BYTEFF2_PYTHON": str(base_bin / "python"),
                    "MONOMER_MD_PYTHON": str(fake_python),
                    "MONOMER_MD_WORKER_UDS": str(socket),
                    "MONOMER_MD_WORKER_MODE": "real",
                    "CAPTURE": str(capture),
                }
            )
            completed = subprocess.run(
                [str(WORKER_SCRIPT)],
                cwd=REPOSITORY_ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((capture / "path").read_text().startswith(f"{base_bin}:"))
            self.assertEqual(
                (capture / "pythonpath").read_text(),
                f"{byteff2_root}:{byteff2_root}/submodules/bytemol",
            )
            self.assertEqual(
                (capture / "args").read_text(),
                f"-m uvicorn app.main:app --uds {socket}",
            )
            self.assertEqual((capture / "socket-dir-mode").read_text(), "700\n")
            self.assertEqual(stat.S_IMODE((capture / "umask-probe").stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()

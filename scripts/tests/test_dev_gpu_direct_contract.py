from __future__ import annotations

import ast
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")


def _shell_function(name: str) -> str:
    source = _read("scripts/dev_server_gpu.sh")
    start = source.index(f"{name}() {{")
    end = source.index("\n}\n", start) + len("\n}\n")
    return source[start:end]


def _python_member(relative: str, *qualified_name: str) -> str:
    source = _read(relative)
    nodes: list[ast.AST] = list(ast.parse(source).body)
    selected: ast.AST | None = None
    for part in qualified_name:
        selected = next(
            (
                node
                for node in nodes
                if isinstance(
                    node,
                    (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                )
                and node.name == part
            ),
            None,
        )
        if selected is None:
            raise AssertionError(
                f"{'.'.join(qualified_name)} is missing from {relative}"
            )
        nodes = list(getattr(selected, "body", ()))
    segment = ast.get_source_segment(source, selected)
    if segment is None:
        raise AssertionError(
            f"{'.'.join(qualified_name)} has no source segment in {relative}"
        )
    return segment


class DevGpuDirectContractTests(unittest.TestCase):
    def test_control_is_registered_only_by_the_9001_development_overlay(
        self,
    ) -> None:
        example = _read(".env.dev.example")
        overlay = _read("docker-compose.dev-gpu-launcher.yml")
        settings = _read("backend/app/config.py")
        backend = _read("backend/app/main.py")
        frontend = _read("frontend/src/components/AppShell.tsx")
        production = _read("docker-compose.prod.yml")

        self.assertIn("NEXPOLY_DEV_GPU_LAUNCHER_ENABLED=false", example)
        self.assertIn('DEV_GPU_OPERATOR_ENABLED: "true"', overlay)
        self.assertIn("DEV_GPU_OPERATOR_FRONTEND_PORT:", overlay)
        self.assertIn('VITE_DEV_GPU_SESSION_CONTROL: "true"', overlay)
        self.assertIn("self.dev_gpu_operator_frontend_port != 9001", settings)
        self.assertIn(
            "if app_settings.dev_gpu_operator_enabled:\n"
            "        app.include_router(dev_gpu_session_router)",
            backend,
        )
        self.assertIn("import.meta.env.DEV &&", frontend)
        self.assertIn(
            'import.meta.env.VITE_DEV_GPU_SESSION_CONTROL === "true"',
            frontend,
        )
        self.assertNotIn("DEV_GPU_OPERATOR_ENABLED", production)
        self.assertNotIn("VITE_DEV_GPU_SESSION_CONTROL", production)

    def test_operator_to_mps_chain_uses_only_the_internal_gpu1_direct_flags(
        self,
    ) -> None:
        operator = _python_member(
            "scripts/dev_gpu_operator.py",
            "DevGpuOperator",
            "_run_recovery",
        )
        shell = _shell_function("gpu_session_up")
        broker_start = _python_member(
            "scripts/dev_gpu_session.py",
            "SessionController",
            "_start_broker",
        )
        mps_start = _python_member(
            "scripts/dev_gpu_session.py",
            "SessionController",
            "_mps_command",
        )
        broker_cli = _python_member(
            "ops/gpu_broker/server.py",
            "parse_args",
        )
        mps_control = _read("scripts/gpu_mps_control.sh")

        self.assertIn(
            'environment["NEXPOLY_DEV_GPU_DIRECT_START"] = "1"',
            operator,
        )
        for command in ("up", "stabilize", "activate"):
            self.assertIn(command, shell)
        self.assertIn("--direct-start", shell)
        self.assertIn("--trusted-dev-gpu-index", broker_start)
        self.assertIn("str(GPU_INDEX)", broker_start)
        self.assertIn("--trusted-dev-start", mps_start)
        self.assertIn(
            'choices=(1,)',
            broker_cli,
        )
        self.assertIn(
            'if [[ "$trusted_dev_start" == "1" && "$index" != "1" ]]',
            mps_control,
        )

    def test_direct_ready_loop_has_no_host_inventory_or_periodic_audit(self) -> None:
        direct_loop = _python_member(
            "scripts/dev_gpu_session.py",
            "SessionController",
            "_direct_serve_loop",
        )
        controller_run = _python_member(
            "scripts/dev_gpu_session.py",
            "SessionController",
            "run",
        )

        for forbidden in (
            "self._audit(",
            "collect_target_snapshot",
            "require_double_free_audit",
            "read_gpu3_guard_fingerprint",
            "ExternalGpuGuard",
            "query_compute_processes",
            "query_docker_gpu_claims",
            "query_systemd_gpu_claims",
        ):
            self.assertNotIn(forbidden, direct_loop)
        self.assertIn('self._state("starting"', controller_run)
        self.assertIn('"plane-ready"', controller_run)
        self.assertIn('"ready"', direct_loop)
        self.assertIn('audit_sequence=0', direct_loop)
        self.assertIn('broker_status.get("leases")', direct_loop)
        self.assertNotIn("for lease in", direct_loop)

    def test_direct_shell_replaces_the_cpu_backend_only_with_the_gpu_backend(
        self,
    ) -> None:
        shell = _shell_function("gpu_session_up")

        self.assertLess(
            shell.index("verify_direct_cpu_backend_baseline"),
            shell.index("validate_asset_release"),
        )
        self.assertEqual(
            shell.count(
                '"${GPU_COMPOSE[@]}" up -d --no-deps --force-recreate backend'
            ),
            1,
        )
        self.assertEqual(
            shell.count(
                '"${COMPOSE[@]}" up -d --no-deps --force-recreate backend'
            ),
            1,
        )
        self.assertIn(
            'if [[ "${NEXPOLY_DEV_GPU_DIRECT_START:-0}" != "1" ]]',
            shell,
        )
        self.assertNotIn("sleep 35", shell)
        self.assertNotIn("ensure_cpu_backend_baseline", shell)

    def test_direct_interfaces_do_not_enter_production_gpu2_paths(self) -> None:
        forbidden = (
            "NEXPOLY_DEV_GPU_DIRECT_START",
            "--direct-start",
            "--trusted-dev-start",
            "--trusted-dev-gpu-index",
            "DEV_GPU_OPERATOR_ENABLED",
        )
        production_paths = (
            "docker-compose.prod.yml",
            "docker-compose.gpu-governed.yml",
            "ops/systemd/nexpoly-gpu2-guard.service",
            "ops/systemd/nexpoly-gpu2-guard.timer",
            "ops/systemd/nexpoly-monomer-dft-worker.service",
            "scripts/gpu2_guard.py",
            "scripts/preflight_monomer_dft_prod.py",
            "scripts/production_readiness.py",
        )

        for relative in production_paths:
            source = _read(relative)
            for token in forbidden:
                self.assertNotIn(token, source, f"{token} leaked into {relative}")


if __name__ == "__main__":
    unittest.main()

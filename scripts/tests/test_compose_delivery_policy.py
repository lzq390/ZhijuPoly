from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DIGEST_A = "ghcr.io/lzq390/nexpoly-backend@sha256:" + "a" * 64
DIGEST_B = "ghcr.io/lzq390/nexpoly-web@sha256:" + "b" * 64


class ComposeDeliveryPolicyTests(unittest.TestCase):
    def test_nexpoly_compose_does_not_set_global_outbound_proxy(self) -> None:
        compose = "\n".join(
            path.read_text(encoding="utf-8")
            for path in REPOSITORY_ROOT.glob("docker-compose*.yml")
        )
        for variable in (
            "HTTP_PROXY:",
            "HTTPS_PROXY:",
            "ALL_PROXY:",
            "http_proxy:",
            "https_proxy:",
            "all_proxy:",
        ):
            self.assertNotIn(variable, compose)

    def test_frontend_image_defaults_to_dormant_openscience_workspace(self) -> None:
        dockerfile = (REPOSITORY_ROOT / "frontend" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        dev_compose = (REPOSITORY_ROOT / "docker-compose.dev.yml").read_text(
            encoding="utf-8"
        )
        frontend_env = (REPOSITORY_ROOT / "frontend" / ".env.example").read_text(
            encoding="utf-8"
        )
        dev_env = (REPOSITORY_ROOT / ".env.dev.example").read_text(encoding="utf-8")

        argument = 'ARG VITE_AGENT_WORKSPACE_URL=""'
        environment = 'ENV VITE_AGENT_WORKSPACE_URL="${VITE_AGENT_WORKSPACE_URL}"'
        build = "RUN npm run build"

        self.assertIn(argument, dockerfile)
        self.assertIn(environment, dockerfile)
        self.assertLess(dockerfile.index(environment), dockerfile.index(build))
        self.assertIn(
            'VITE_AGENT_WORKSPACE_URL: "${VITE_AGENT_WORKSPACE_URL:-}"',
            compose,
        )
        self.assertIn(
            'VITE_AGENT_WORKSPACE_URL: "${VITE_AGENT_WORKSPACE_URL:-}"',
            dev_compose,
        )
        self.assertIn("VITE_AGENT_WORKSPACE_URL=", frontend_env)
        self.assertIn("VITE_AGENT_WORKSPACE_URL=", dev_env)
        self.assertIn("http://127.0.0.1:9011/", frontend_env + dev_env)
        self.assertNotIn("4454", dockerfile + compose + dev_compose + frontend_env + dev_env)

    def test_blank_ci_database_and_production_takeover_use_distinct_migration_modes(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        controller = (REPOSITORY_ROOT / "scripts" / "pull_deploy_controller.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("python -m app.postgres_migrations --mode bootstrap", workflow)
        self.assertNotIn(
            "python -m app.postgres_migrations --mode bootstrap-expand",
            workflow,
        )
        self.assertIn('"bootstrap-expand"', controller)
        self.assertIn('descriptor["previous_deployment"] is None', controller)
        self.assertIn('else "expand"', controller)

    def test_tracked_compose_contract_has_no_production_default_password(self) -> None:
        base = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        production = (REPOSITORY_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
        dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertNotIn("polyprop:polyprop", base + production)
        self.assertIn('127.0.0.1:${NEXPOLY_POSTGRES_PORT:-55432}:5432', base)
        self.assertIn("!reset null", production)
        self.assertIn("!override", production)
        self.assertNotIn("docker compose build", production)
        self.assertNotIn("COPY model", (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8"))
        for local_runtime in (
            "/.runtime/",
            "/.venv-monomer-md-worker/",
            "/.venv-monomer-md-worker.staging-*/",
            "/.venv-monomer-md-worker.previous-*/",
        ):
            self.assertIn(local_runtime, dockerignore)

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose is not available")
    def test_production_override_renders_only_digest_application_images_and_no_build(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            app_env = temporary / "app.env"
            app_env.write_text("ONLINE_KNOWLEDGE_API_KEY=\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "NEXPOLY_BACKEND_IMAGE": DIGEST_A,
                    "NEXPOLY_WEB_IMAGE": DIGEST_B,
                    "NEXPOLY_POSTGRES_PASSWORD": "test-only-not-production",
                    "APP_POSTGRES_DSN": "postgresql://nexpoly:test@lab-postgres:5432/nexpoly",
                    "PI_POSTGRES_DSN": "postgresql://nexpoly:test@lab-postgres:5432/nexpoly",
                    "LAB_DATA_POSTGRES_DSN": "postgresql://nexpoly:test@lab-postgres:5432/nexpoly",
                    "NEXPOLY_APP_ENV_FILE": str(app_env),
                    "NEXPOLY_ASSET_ROOT": str(temporary / "assets"),
                    "NEXPOLY_RUNTIME_ROOT": "/data/lzq/gith/nexpoly-runtime",
                    "POLYTAO_ENABLED": "true",
                    "WEB_CONCURRENCY": "9",
                    "GEN_JOB_WORKERS": "9",
                    "POLYTAO_JOB_THREADS": "9",
                    "POLYTAO_MAX_ACTIVE_JOBS": "9",
                }
            )
            result = subprocess.run(
                [
                    "docker", "compose", "-p", "nexpoly", "-f", str(REPOSITORY_ROOT / "docker-compose.yml"),
                    "-f", str(REPOSITORY_ROOT / "docker-compose.prod.yml"), "config", "--format", "json",
                ],
                env=environment, text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(result.stdout)
            for service in ("postgres-init", "backend", "nginx"):
                self.assertNotIn("build", document["services"][service])
            self.assertEqual(document["services"]["backend"]["image"], DIGEST_A)
            self.assertEqual(document["services"]["nginx"]["image"], DIGEST_B)
            self.assertEqual(document["services"]["lab-postgres"]["ports"][0]["host_ip"], "127.0.0.1")
            backend = document["services"]["backend"]
            self.assertEqual(backend["environment"]["WEB_CONCURRENCY"], "1")
            self.assertEqual(backend["environment"]["GEN_JOB_WORKERS"], "1")
            self.assertEqual(backend["environment"]["POLYTAO_JOB_THREADS"], "1")
            self.assertEqual(backend["environment"]["POLYTAO_MAX_ACTIVE_JOBS"], "1")
            self.assertEqual(
                backend["environment"]["MONOMER_MD_CANARY_STATE_DIR"],
                "/app/monomer-md-canaries",
            )
            backend_volumes = {
                volume["target"]: volume for volume in backend["volumes"]
            }
            model_volume = backend_volumes["/app/model"]
            self.assertEqual(
                model_volume["source"],
                str(temporary / "assets" / "model"),
            )
            self.assertTrue(model_volume["read_only"])
            canary_volume = backend_volumes["/app/monomer-md-canaries"]
            self.assertEqual(
                canary_volume["source"],
                "/data/lzq/gith/nexpoly-runtime/state/monomer-md-canaries",
            )
            self.assertFalse(canary_volume.get("read_only", False))
            self.assertNotIn(
                "/app/monomer-md-canaries",
                {
                    volume["target"]
                    for volume in document["services"]["postgres-init"]["volumes"]
                },
            )
            self.assertIn(backend["stop_grace_period"], ("45s", 45000000000))


if __name__ == "__main__":
    unittest.main()

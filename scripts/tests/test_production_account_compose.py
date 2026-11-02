from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("docker"), "Docker Compose is unavailable")
class ProductionAccountComposeTests(unittest.TestCase):
    def render(self, **overrides):
        with tempfile.NamedTemporaryFile(mode="w") as app_env:
            # Values in env_file must not accidentally override this release's
            # explicit policy, including when copied from the old deployment.
            app_env.write("ALLOWED_ORIGINS=http://localhost:9000\n"
                          "USAGE_ENTITLEMENTS_ENABLED=true\n")
            app_env.flush()
            env = {"PATH": os.environ["PATH"],
                   "NEXPOLY_BACKEND_IMAGE": "ghcr.io/lzq390/nexpoly-backend@sha256:" + "1" * 64,
                   "NEXPOLY_WEB_IMAGE": "ghcr.io/lzq390/nexpoly-web@sha256:" + "2" * 64,
                   "NEXPOLY_POSTGRES_PASSWORD": "test-only",
                   "NEXPOLY_PROD_PG_VOLUME": "nexpoly_prod_candidate_test",
                   "NEXPOLY_ASSET_ROOT": "/tmp/production-assets",
                   "NEXPOLY_RUNTIME_ROOT": "/tmp/production-candidate",
                   "NEXPOLY_POLYMER_MD_RUNTIME_ROOT": "/tmp/production-pmd",
                   "NEXPOLY_POLYMER_MD_SOCKET_ROOT": "/tmp/production-pmd-socket",
                   "NEXPOLY_MD_SOCKET_ROOT": "/tmp/production-md-socket",
                   "NEXPOLY_DFT_SOCKET_ROOT": "/tmp/production-dft-socket",
                   "NEXPOLY_DFT_SPOOL_ROOT": "/tmp/production-dft-spool",
                   "NEXPOLY_SERVICE_UID": "1001", "NEXPOLY_SERVICE_GID": "1001",
                   "NEXPOLY_APP_ENV_FILE": app_env.name,
                   "NEXPOLY_BATCH_ENV_FILE": os.devnull,
                   "NEXPOLY_BATCH_POSTGRES_DSN": "postgresql://batch:test@lab-postgres/nexpoly",
                   "APP_POSTGRES_DSN": "postgresql://api:test@lab-postgres/nexpoly",
                   "PI_POSTGRES_DSN": "postgresql://api:test@lab-postgres/nexpoly",
                   "LAB_DATA_POSTGRES_DSN": "postgresql://api:test@lab-postgres/nexpoly",
                   "POLYTAO_ENABLED": "true", **overrides}
            command = ["docker", "compose", "--env-file", os.devnull,
                       "-p", "nexpoly_production_account_test"]
            for name in ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.prod-accounts.yml"):
                command.extend(["-f", str(ROOT / name)])
            command.extend(["config", "--format", "json"])
            return json.loads(subprocess.check_output(command, cwd=ROOT, env=env, text=True))

    def test_candidate_is_private_and_preserves_data_with_exact_auth_policy(self):
        config = self.render()
        services = config["services"]
        backend = services["backend"]
        self.assertEqual(backend["user"], "1001:1001")
        env = backend["environment"]
        self.assertEqual(env["ALLOWED_ORIGINS"], "http://114.214.255.154:9000")
        self.assertEqual(env["AUTH_COOKIE_NAME"], "nexpoly_prod_http_session")
        self.assertEqual(env["AUTH_PROD_HTTP_ENABLED"], "true")
        self.assertEqual(env["USAGE_METERING_ENABLED"], "true")
        for key in ("USAGE_ENTITLEMENTS_ENABLED", "MONOMER_DFT_ARTIFACT_RETENTION_ENABLED",
                    "POLYMER_MD_RETENTION_ENABLED", "MONOMER_POLYMERIZATION_BATCH_RETENTION_ENABLED"):
            self.assertEqual(env[key], "false")
        for name, port in (("backend", "18002"), ("nginx", "19000"), ("lab-postgres", "55433")):
            self.assertEqual(services[name]["ports"][0]["host_ip"], "127.0.0.1")
            self.assertEqual(services[name]["ports"][0]["published"], port)
        self.assertNotIn("postgres-init", backend["depends_on"])
        self.assertTrue(config["volumes"]["app_postgres_data"]["external"])
        self.assertNotIn("config", config["networks"]["default"].get("ipam", {}))
        self.assertIn("--schema-target literature-0024 --service-context", backend["healthcheck"]["test"][1])
        for mount in backend["volumes"]:
            if mount["target"] in {"/app/polymer-md-socket", "/app/polymer-md-artifacts", "/app/model"}:
                self.assertTrue(mount["read_only"])
        batch = services["polymerization-batch-worker"]
        self.assertEqual(batch["cpus"], 1.0)
        self.assertEqual(batch["mem_limit"], "3221225472")
        self.assertEqual(batch["environment"]["USAGE_ENTITLEMENTS_ENABLED"], "false")
        self.assertEqual(batch["environment"]["APP_SERVICE_POSTGRES_DSN"], "postgresql://batch:test@lab-postgres/nexpoly")
        self.assertEqual(batch["environment"]["AUTH_POSTGRES_DSN"], "")
        self.assertEqual(batch["environment"]["POLYMER_MD_PROD_ENABLED"], "false")
        self.assertEqual(batch["environment"]["POLYMER_MD_SUBMIT_ENABLED"], "false")
        self.assertEqual(batch["environment"]["MONOMER_DFT_SUBMIT_ENABLED"], "false")

    def test_explicit_cutover_only_changes_web_binding(self):
        services = self.render(NEXPOLY_WEB_BIND="0.0.0.0", NEXPOLY_WEB_PORT="9000")["services"]
        self.assertEqual(services["nginx"]["ports"][0]["published"], "9000")
        self.assertEqual(services["nginx"]["ports"][0]["host_ip"], "0.0.0.0")
        self.assertEqual(services["backend"]["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual(services["backend"]["environment"]["ALLOWED_ORIGINS"], "http://114.214.255.154:9000")

    def test_batch_can_start_without_web_auth_or_science_tokens(self):
        from backend.app.config import Settings

        environment = self.render()["services"]["polymerization-batch-worker"]["environment"]
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings()
            self.assertFalse(settings.polymer_md_enabled)
            self.assertFalse(settings.polymer_md_submit_enabled)
            self.assertFalse(settings.monomer_dft_submit_enabled)


if __name__ == "__main__":
    unittest.main()

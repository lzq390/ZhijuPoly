from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
REGISTRY_TARGET = "/app/polymer-md-acceptance"


@unittest.skipUnless(shutil.which("docker"), "Docker Compose is not available")
class PolymerMdRegistryComposeTests(unittest.TestCase):
    def configuration(self, *, registry=False, values=None):
        environment = dict(os.environ)
        for key in ("POLYMER_MD_ACCEPTANCE_ROOT", "POLYMER_MD_ACCEPTANCE_HOST_ROOT",
                    "POLYMER_MD_ACCEPTANCE_UID", "POLYMER_MD_ACCEPTANCE_GID"):
            environment.pop(key, None)
        environment.update({
            "NEXPOLY_ASSET_ROOT": "/tmp/fixture-pmd-assets",
            "POLYMER_MD_SOCKET_DIR": "/tmp/fixture-pmd-socket",
            "POLYMER_MD_JOB_ROOT": "/tmp/fixture-pmd-jobs",
            "POLYMER_MD_START_AUTHORIZATION_TOKEN": "fixture-pmd-token",
            "POLYMER_MD_SUBMIT_ENABLED": "false",
            "APP_POSTGRES_DSN": "postgresql://api:fixture@lab-postgres:5432/fixture",
            "APP_SERVICE_POSTGRES_DSN": "postgresql://service:fixture@lab-postgres:5432/fixture",
            "AUTH_POSTGRES_DSN": "postgresql://auth:fixture@lab-postgres:5432/fixture",
            "AUTH_ADMIN_POSTGRES_DSN": "postgresql://admin:fixture@lab-postgres:5432/fixture",
            "MONOMER_DFT_START_AUTHORIZATION_TOKEN": "fixture-dft-token",
        })
        environment.update(values or {})
        command = ["docker", "compose", "-p", "polymer_md_registry_config_test"]
        names = ["docker-compose.yml", "docker-compose.dev.yml",
                 "docker-compose.user-isolation-dev.yml", "docker-compose.polymer-md-dev.yml"]
        if registry:
            names.append("docker-compose.polymer-md-registry-dev.yml")
        for name in names:
            command.extend(["-f", str(ROOT / name)])
        # Configuration parsing only: no daemon API, services or actual registry.
        return subprocess.run(command + ["config", "--format", "json", "--no-env-resolution"],
                              env=environment, text=True, capture_output=True)

    def test_default_md_overlay_has_no_registry_or_new_admission(self):
        result = self.configuration()
        self.assertEqual(result.returncode, 0, result.stderr)
        backend = json.loads(result.stdout)["services"]["backend"]
        self.assertNotIn("POLYMER_MD_ACCEPTANCE_ROOT", backend["environment"])
        self.assertNotIn(REGISTRY_TARGET, [v["target"] for v in backend["volumes"]])
        self.assertEqual(backend["environment"]["POLYMER_MD_SUBMIT_ENABLED"], "false")

    def test_optional_registry_is_private_uid_matched_readonly_and_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "not-created-by-compose-config"
            result = self.configuration(registry=True, values={
                "POLYMER_MD_ACCEPTANCE_HOST_ROOT": str(missing),
                "POLYMER_MD_ACCEPTANCE_UID": "1001", "POLYMER_MD_ACCEPTANCE_GID": "1001",
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            services = json.loads(result.stdout)["services"]
            backend = services["backend"]
            self.assertEqual(backend["user"], "1001:1001")
            self.assertEqual(backend["environment"]["POLYMER_MD_ACCEPTANCE_ROOT"], REGISTRY_TARGET)
            self.assertEqual(backend["environment"]["POLYMER_MD_SUBMIT_ENABLED"], "false")
            mounts = [v for v in backend["volumes"] if v["target"] == REGISTRY_TARGET]
            self.assertEqual(len(mounts), 1)
            self.assertEqual(mounts[0]["source"], str(missing))
            self.assertTrue(mounts[0]["read_only"])
            # New Compose JSON omits false bind options. Independently require
            # the source to opt out explicitly before accepting that omission.
            overlay = (ROOT / "docker-compose.polymer-md-registry-dev.yml").read_text()
            self.assertIn(
                f"        target: {REGISTRY_TARGET}\n"
                "        read_only: true\n"
                "        bind:\n"
                "          create_host_path: false\n",
                overlay,
            )
            self.assertIs(mounts[0].get("bind", {}).get("create_host_path", False), False)
            self.assertFalse(missing.exists())
            for name, service in services.items():
                if name != "backend":
                    self.assertNotIn("POLYMER_MD_ACCEPTANCE_ROOT", service.get("environment", {}))
                    self.assertNotIn(REGISTRY_TARGET, [v["target"] for v in service.get("volumes", [])])

    def test_opt_in_requires_existing_host_path_setting_and_explicit_uid_gid(self):
        values = {"POLYMER_MD_ACCEPTANCE_HOST_ROOT": "/tmp/fixture-registry",
                  "POLYMER_MD_ACCEPTANCE_UID": "1001", "POLYMER_MD_ACCEPTANCE_GID": "1001"}
        for missing in values:
            with self.subTest(missing=missing):
                result = self.configuration(registry=True,
                    values={key: value for key, value in values.items() if key != missing})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(missing, result.stderr)


if __name__ == "__main__":
    unittest.main()

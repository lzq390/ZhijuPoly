#!/usr/bin/env python3
"""Run MCP tests against a disposable PostgreSQL, never an application DSN."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=str(ROOT / ".runtime/property-filter-mcp/venv/bin/python"))
    args = parser.parse_args()
    # Preserve a virtual environment's interpreter symlink: resolving it would
    # launch the base Python without the independent MCP test dependencies.
    interpreter = Path(os.path.abspath(args.python))
    if not interpreter.is_file():
        parser.error("Install the independent MCP test lock into the documented virtual environment first.")
    root = ROOT / ".runtime/property-filter-mcp"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    container = "nexpoly-mcp-test-" + uuid4().hex[:12]
    created = False
    with tempfile.TemporaryDirectory(prefix="test-", dir=root) as directory:
        private = Path(directory)
        password = secrets.token_urlsafe(32)
        envfile = private / "postgres.env"
        envfile.write_text("POSTGRES_USER=mcp_test_admin\nPOSTGRES_DB=mcp_test_admin\nPOSTGRES_PASSWORD=" + password + "\n")
        envfile.chmod(0o600)
        try:
            subprocess.run(["docker", "run", "--rm", "-d", "--name", container,
                            "--label", "com.nexpoly.purpose=property-filter-mcp-test",
                            "--env-file", str(envfile), "-p", "127.0.0.1::5432", IMAGE],
                           check=True, stdout=subprocess.DEVNULL)
            created = True
            for _ in range(60):
                ready = subprocess.run(["docker", "exec", container, "pg_isready", "-U", "mcp_test_admin"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if ready.returncode == 0:
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError("Disposable MCP test PostgreSQL did not become ready")
            ports = json.loads(subprocess.check_output(
                ["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", container], text=True))
            published = ports["5432/tcp"][0]
            if published["HostIp"] != "127.0.0.1":
                raise RuntimeError("Test database must publish only on loopback")
            dsnfile = private / "admin.dsn"
            dsnfile.write_text(f"postgresql://mcp_test_admin:{password}@127.0.0.1:{published['HostPort']}/mcp_test_admin\n")
            dsnfile.chmod(0o600)
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(("NEXPOLY_MCP_", "APP_POSTGRES_", "PI_POSTGRES_", "AUTH_POSTGRES_"))}
            environment.update(NEXPOLY_MCP_TEST_ADMIN_DSN_FILE=str(dsnfile), PYTHONPATH=str(ROOT / "backend"))
            return subprocess.run([str(interpreter), "-m", "pytest", "-c", "backend/mcp_tests/pytest.ini",
                                   "backend/mcp_tests", "-q", "--tb=short"], cwd=ROOT, env=environment).returncode
        finally:
            if created:
                subprocess.run(["docker", "stop", "--time", "5", container],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run isolated retrosynthesis MCP HTTP/SDK tests without accounts, DB or GPU."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=str(ROOT / ".runtime/retrosynthesis-mcp/venv/bin/python"))
    args = parser.parse_args()
    interpreter = Path(os.path.abspath(args.python))
    if not interpreter.is_file():
        parser.error("Install backend/requirements-retrosynthesis-mcp-test.lock in an independent virtual environment first.")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("NEXPOLY_RETROSYNTHESIS_MCP_", "APP_POSTGRES_", "AUTH_POSTGRES_"))}
    environment["PYTHONPATH"] = str(ROOT / "backend")
    return subprocess.run([str(interpreter), "-m", "pytest", "-c", "backend/retrosynthesis_mcp_tests/pytest.ini",
                           "backend/retrosynthesis_mcp_tests", "--tb=short"], cwd=ROOT, env=environment).returncode


if __name__ == "__main__":
    raise SystemExit(main())

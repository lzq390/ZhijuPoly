"""Opt-in pytest gate: required isolation coverage cannot pass by being skipped."""
from __future__ import annotations

import os
from pathlib import Path

REQUIRED = {
    "test_auth_isolation.py", "test_auth_settings.py", "test_auth_stream_revocation.py",
    "test_task_events.py", "test_task_events_postgres.py",
    "test_private_asset_isolation.py", "test_private_assets_postgres.py",
    "test_monomer_user_isolation_postgres.py", "test_multiuser_contract.py",
    "test_multiuser_restore.py",
    "test_user_isolation_release_contract.py",
}


def pytest_configure(config):
    config._multiuser_skips = []


def pytest_runtest_logreport(report):
    # Kept as a module list because pytest's report hook has no config argument.
    if report.skipped and Path(report.nodeid.split("::", 1)[0]).name in REQUIRED:
        _skipped.append(report.nodeid)


def pytest_collectreport(report):
    if report.skipped and Path(report.nodeid.split("::", 1)[0]).name in REQUIRED:
        _skipped.append(report.nodeid)


_skipped: list[str] = []


def pytest_sessionstart(session):
    _skipped.clear()


def pytest_sessionfinish(session, exitstatus):
    if os.getenv("NEXPOLY_MULTIUSER_REQUIRED") == "1" and _skipped:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter):
    if os.getenv("NEXPOLY_MULTIUSER_REQUIRED") == "1" and _skipped:
        terminalreporter.write_sep("=", "Required multi-user coverage was skipped", red=True)
        for nodeid in _skipped:
            terminalreporter.write_line(nodeid)

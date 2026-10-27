import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from workers.polymer_md_worker.app.journal import atomic_json, read_json, safe_file
from workers.polymer_md_worker.app.process_control import ScopeControl, scientific_environment


def test_scope_entry_cannot_execute_science_without_exact_gate(tmp_path):
    marker = tmp_path / "must-not-exist"
    command = [sys.executable, "-m", "workers.polymer_md_worker.app.execution_gate",
               "--identity", str(tmp_path / "entry.json"), "--gate", str(tmp_path / "gate.json"),
               "--unit", "nexpoly-polymer-md-test.scope", "--nonce", "f" * 32,
               "--expires", str(time.time() + .1), "--", sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
    result = subprocess.run(command, env=scientific_environment(), capture_output=True, timeout=5)
    assert result.returncode == 125 and not marker.exists()
    assert read_json(tmp_path / "entry.json")["nonce"] == "f" * 32


def test_atomic_journal_and_private_path_boundary(tmp_path):
    path = tmp_path / "journal.json"
    atomic_json(path, {"sequence": 1})
    assert read_json(path) == {"sequence": 1}
    assert path.stat().st_mode & 0o777 == 0o600
    (tmp_path / "link").symlink_to(path)
    with pytest.raises(ValueError):
        safe_file(tmp_path, "link")
    with pytest.raises(ValueError):
        safe_file(tmp_path, "../journal.json")


def test_scientific_subprocess_has_no_backend_credentials(monkeypatch):
    monkeypatch.setenv("APP_SERVICE_POSTGRES_DSN", "sensitive")
    monkeypatch.setenv("POLYMER_MD_START_AUTHORIZATION_TOKEN", "sensitive")
    monkeypatch.setenv("DATABASE_URL", "sensitive")
    monkeypatch.setenv("PGPASSWORD", "sensitive")
    monkeypatch.setenv("POLYMER_MD_RUNTIME_MANIFEST", "/private/manifest.json")
    environment = scientific_environment()
    assert "APP_SERVICE_POSTGRES_DSN" not in environment
    assert "POLYMER_MD_START_AUTHORIZATION_TOKEN" not in environment
    assert "DATABASE_URL" not in environment and "PGPASSWORD" not in environment
    assert environment["POLYMER_MD_RUNTIME_MANIFEST"] == "/private/manifest.json"
    assert environment["CUDA_VISIBLE_DEVICES"] == ""


def test_scientific_import_never_writes_shared_dependency_bytecode(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "0")
    dependency = tmp_path / "shared_dependency.py"
    dependency.write_text("VALUE = 7\n")
    environment = scientific_environment()
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
         "import shared_dependency; assert shared_dependency.VALUE == 7", str(tmp_path)],
        env=environment, capture_output=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "__pycache__").exists()


def test_real_scope_contains_and_kills_detached_descendants(tmp_path):
    scope = ScopeControl()
    if not scope.probe()["ready"]:
        pytest.skip("systemd user scopes unavailable in this test host")
    unit = f"nexpoly-polymer-md-test-{uuid4().hex}.scope"
    script = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True);time.sleep(60)"
    with (tmp_path / "scope.log").open("wb") as log:
        process = scope.launch(unit=unit, command=[sys.executable, "-c", script], cwd=tmp_path,
                               log=log, environment=scientific_environment())
        try:
            group = None
            for _ in range(100):
                state = scope.inspect(unit)
                group = state.get("ControlGroup")
                if group:
                    break
                time.sleep(0.02)
            assert group and unit in group
            assert not scope.confirm_gone(unit, group)
            assert scope.terminate(unit, group)
            process.wait(timeout=5)
            assert scope.confirm_gone(unit, group)
        finally:
            scope.terminate(unit, group)
            process.wait(timeout=5)

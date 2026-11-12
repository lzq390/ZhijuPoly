from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import server_monitoring_9003 as deploy


def vite_identity(pid: int, cwd: Path, *, parent=1):
    return {"pid": pid, "parent": parent, "start_ticks": 12345,
            "executable": str(cwd / "bin/node"), "cwd": str(cwd),
            "arguments": [str(cwd / "bin/node"), "node_modules/vite/bin/vite.js",
                          "--host", "0.0.0.0", "--port", "9003", "--strictPort"]}


def test_process_identity_observes_only_current_process():
    identity = deploy.process_identity(os.getpid())
    assert identity["pid"] == os.getpid()
    assert identity["parent"] == os.getppid()
    assert identity["start_ticks"] > 0
    assert identity["cwd"] == str(Path.cwd())


@pytest.mark.parametrize("version,allowed", [
    ("v18.20.8", False), ("v20.18.0", False), ("v20.19.0", True),
    ("v21.9.0", False), ("v22.11.0", False), ("v22.12.0", True),
    ("v22.23.2", True), ("v24.1.0", True), ("unknown", False),
])
def test_node_version_must_satisfy_vite_engine(monkeypatch, tmp_path, version, allowed):
    binary = tmp_path / "node"
    binary.touch()
    monkeypatch.setattr(deploy.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=version + "\n"))
    if allowed:
        assert deploy.compatible_node(binary) == str(binary)
    else:
        with pytest.raises(RuntimeError):
            deploy.compatible_node(binary)


def test_unsupported_path_node_requires_an_explicit_compatible_binary(monkeypatch, tmp_path):
    binary = tmp_path / "node18"
    binary.touch()
    monkeypatch.setattr(deploy.shutil, "which", lambda _: str(binary))
    monkeypatch.setattr(deploy.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="v18.20.8\n"))
    with pytest.raises(RuntimeError, match="prepare --node"):
        deploy.compatible_node()


@pytest.mark.parametrize("output", [
    "State Recv-Q Send-Q Local Address:Port Peer Address:PortProcess\nLISTEN 0 128 0.0.0.0:9003 0.0.0.0:*\n",
    'LISTEN users:(("node",pid=10,fd=1),("node",pid=11,fd=1))',
])
def test_ambiguous_listener_is_not_treated_as_a_free_port(monkeypatch, output):
    monkeypatch.setattr(deploy.subprocess, "check_output", lambda *args, **kwargs: output)
    with pytest.raises(RuntimeError):
        deploy.listening_pid(9003)


@pytest.mark.parametrize("change", ["pid_reused", "exec_changed", "cwd_changed"])
def test_pidfd_recheck_prevents_signalling_changed_identity(monkeypatch, tmp_path, change):
    expected = vite_identity(42, tmp_path)
    actual = {**expected}
    if change == "pid_reused":
        actual["start_ticks"] += 1
    elif change == "exec_changed":
        actual["arguments"] = ["unrelated"]
    else:
        actual["cwd"] = str(tmp_path / "another")
    closed, signals = [], []
    monkeypatch.setattr(deploy.os, "pidfd_open", lambda pid: 123, raising=False)
    monkeypatch.setattr(deploy.os, "close", closed.append)
    monkeypatch.setattr(deploy.signal, "pidfd_send_signal", lambda *args: signals.append(args), raising=False)
    monkeypatch.setattr(deploy, "process_identity", lambda pid: actual)
    with pytest.raises(RuntimeError, match="no signal"):
        deploy.terminate_exact_process(expected)
    assert closed == [123] and signals == []


def test_exact_identity_signal_uses_pidfd_only(monkeypatch, tmp_path):
    expected = vite_identity(42, tmp_path)
    closed, signals = [], []
    monkeypatch.setattr(deploy.os, "pidfd_open", lambda pid: 123, raising=False)
    monkeypatch.setattr(deploy.os, "close", closed.append)
    monkeypatch.setattr(deploy.signal, "pidfd_send_signal", lambda *args: signals.append(args), raising=False)
    monkeypatch.setattr(deploy.os, "kill", lambda *args: pytest.fail("must not signal a bare PID"))
    monkeypatch.setattr(deploy, "process_identity", lambda pid: expected)
    deploy.terminate_exact_process(expected)
    assert closed == [123]
    assert signals == [(123, deploy.signal.SIGTERM)]


@pytest.mark.parametrize("change", ["wrong_cwd", "wrong_port", "extra_args", "wrong_executable"])
def test_preview_command_must_match_exact_source_and_port(tmp_path, change):
    identity = vite_identity(42, tmp_path)
    if change == "wrong_cwd":
        identity["cwd"] = str(tmp_path / "other")
    elif change == "wrong_port":
        identity["arguments"][5] = "9001"
    elif change == "extra_args":
        identity["arguments"].append("unrelated")
    else:
        identity["executable"] = "/usr/bin/python3"
    with pytest.raises(RuntimeError, match="leaving it untouched"):
        deploy.require_vite(identity, tmp_path)


def test_ready_requires_port_owner_to_be_the_managed_unit(monkeypatch):
    monkeypatch.setattr(deploy, "listening_pid", lambda port: 22)
    monkeypatch.setattr(deploy, "unit_preview_pid", lambda: 33)
    with pytest.raises(RuntimeError, match="unrelated process"):
        deploy.wait_managed_preview()


def test_ready_requires_managed_unit_to_use_this_worktree(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "ROOT", tmp_path / "current")
    monkeypatch.setattr(deploy, "listening_pid", lambda port: 22)
    monkeypatch.setattr(deploy, "unit_preview_pid", lambda: 22)
    monkeypatch.setattr(deploy, "process_identity", lambda pid: vite_identity(pid, tmp_path / "other/frontend"))
    with pytest.raises(RuntimeError):
        deploy.wait_managed_preview()


def test_legacy_path_merely_appearing_in_arguments_does_not_authorize_a_signal(monkeypatch, tmp_path):
    launcher = tmp_path / "overview-preview.mjs"
    launcher.write_text("// not running")
    child = vite_identity(200, tmp_path / "frontend", parent=100)
    parent = {**vite_identity(100, tmp_path / "frontend"),
              "arguments": [str(tmp_path / "frontend/bin/node"), "/tmp/unrelated.mjs", str(launcher)]}
    monkeypatch.setattr(deploy, "LEGACY_LAUNCHER", launcher)
    monkeypatch.setattr(deploy.socket, "create_connection", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(deploy, "unit_preview_pid", lambda: 0)
    monkeypatch.setattr(deploy, "listening_pid", lambda port: 200 if port == 9003 else 100)
    monkeypatch.setattr(deploy, "process_identity", lambda pid: child if pid == 200 else parent)
    monkeypatch.setattr(deploy, "terminate_exact_process", lambda _: pytest.fail("must not signal unrelated launcher"))
    monkeypatch.setattr(deploy, "systemctl", lambda *args, **kwargs: pytest.fail("must not alter units"))
    with pytest.raises(RuntimeError, match="unrecognized process"):
        deploy.preview()


def test_takeover_preserves_distinct_launcher_and_vite_directories(monkeypatch, tmp_path):
    launcher = tmp_path / "overview-preview.mjs"
    launcher.write_text("// launcher passes an explicit cwd to Vite")
    child = vite_identity(200, tmp_path / "old/frontend", parent=100)
    parent = {**vite_identity(100, tmp_path / "launcher-cwd"),
              "arguments": [str(tmp_path / "launcher-cwd/bin/node"), str(launcher)]}
    runtime = tmp_path / "runtime"
    ports = {9003: 200, 5318: 100}
    monkeypatch.setattr(deploy, "RUNTIME", runtime)
    monkeypatch.setattr(deploy, "LEGACY_LAUNCHER", launcher)
    monkeypatch.setattr(deploy.socket, "create_connection", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(deploy, "unit_preview_pid", lambda: 0)
    monkeypatch.setattr(deploy, "listening_pid", ports.get)
    monkeypatch.setattr(deploy, "process_identity", lambda pid: child if pid == 200 else parent)
    def terminate(identity):
        assert identity == parent
        ports.update({9003: None, 5318: None})
    monkeypatch.setattr(deploy, "terminate_exact_process", terminate)
    monkeypatch.setattr(deploy, "systemctl", lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy, "wait_managed_preview", lambda: None)
    deploy.preview()
    manifest = json.loads((runtime / "rollback/preview.json").read_text())
    assert manifest["original_cwd"] == parent["cwd"]
    assert manifest["preview_cwd"] == child["cwd"]


@pytest.fixture
def legacy_unit(monkeypatch, tmp_path):
    group = f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/{deploy.LEGACY_UNIT}"
    parent = {**vite_identity(100, tmp_path / "launcher-cwd"), "cgroup": f"0::{group}\n"}
    parent["arguments"] = [parent["executable"], str(deploy.LEGACY_LAUNCHER)]
    child = {**vite_identity(200, tmp_path / "old/frontend", parent=100), "cgroup": parent["cgroup"]}
    fragment = Path(f"/run/user/{os.getuid()}/systemd/transient/{deploy.LEGACY_UNIT}")
    properties = {"Id": deploy.LEGACY_UNIT, "LoadState": "loaded", "ActiveState": "active",
                  "MainPID": "100", "ControlGroup": group, "WorkingDirectory": parent["cwd"],
                  "Transient": "yes", "FragmentPath": str(fragment), "DropInPaths": "",
                  "Type": "simple", "InvocationID": "a" * 32, "Restart": "on-failure",
                  "RestartUSec": "3s", "Description": "Previous preview"}
    content = ('[Unit]\nDescription=Previous preview\n[Service]\nRestart=on-failure\nRestartSec=3s\n'
               f'WorkingDirectory={parent["cwd"]}\nExecStart=\nExecStart="{parent["executable"]}" "{deploy.LEGACY_LAUNCHER}"\n')
    original_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *args, **kwargs: content if self == fragment else original_read(self, *args, **kwargs))
    monkeypatch.setattr(deploy, "legacy_unit_properties", lambda: dict(properties))
    return parent, child, properties, content


def test_capture_binds_transient_unit_and_preserves_complete_content(legacy_unit):
    parent, child, properties, content = legacy_unit
    assert deploy.capture_legacy_unit(parent, child) == {"properties": properties, "content": content}


@pytest.mark.parametrize("failure", ["main_pid", "child_cgroup", "unknown_service", "drop_in", "invocation_change"])
def test_changed_or_unrecognized_unit_cannot_be_captured(monkeypatch, legacy_unit, failure):
    parent, child, properties, _ = legacy_unit
    if failure == "main_pid":
        properties["MainPID"] = "999"
    elif failure == "child_cgroup":
        child["cgroup"] += "unexpected"
    elif failure == "unknown_service":
        parent["cgroup"] = "0::/user.slice/unrelated.service\n"
    elif failure == "drop_in":
        properties["DropInPaths"] = "/tmp/unrelated.conf"
    else:
        changes = iter([dict(properties), {**properties, "InvocationID": "b" * 32}])
        monkeypatch.setattr(deploy, "legacy_unit_properties", lambda: next(changes))
    with pytest.raises(RuntimeError):
        deploy.capture_legacy_unit(parent, child)


def test_verified_service_is_stopped_instead_of_signalling_its_restarting_process(monkeypatch, legacy_unit):
    parent, child, properties, content = legacy_unit
    captured = {"properties": dict(properties), "content": content}
    calls = []
    monkeypatch.setattr(deploy, "process_identity", lambda pid: parent if pid == 100 else child)
    monkeypatch.setattr(deploy, "systemctl", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(deploy, "terminate_exact_process", lambda _: pytest.fail("service-managed launcher must not receive bare process termination"))
    deploy.stop_legacy_unit(captured, parent, child)
    assert calls == [("stop", deploy.LEGACY_UNIT)]


def test_restarted_service_is_not_stopped_using_stale_capture(monkeypatch, legacy_unit):
    parent, child, properties, content = legacy_unit
    captured = {"properties": dict(properties), "content": content}
    properties["InvocationID"] = "b" * 32
    monkeypatch.setattr(deploy, "systemctl", lambda *args, **kwargs: pytest.fail("must not stop a replaced unit"))
    with pytest.raises(RuntimeError, match="no service stopped"):
        deploy.stop_legacy_unit(captured, parent, child)


def test_rollback_recreates_the_saved_transient_restart_policy(monkeypatch, tmp_path):
    unit = tmp_path / "legacy.service"
    unit.write_text("[Service]\nRestart=on-failure\n")
    launcher = tmp_path / "saved-launcher.mjs"
    manifest = {"legacy_unit": {"name": deploy.LEGACY_UNIT, "backup": str(unit),
                               "sha256": hashlib.sha256(unit.read_bytes()).hexdigest(), "description": "Previous preview"},
                "original_command": ["/reviewed/node", "/old/launcher.mjs"], "original_cwd": "/reviewed/cwd"}
    monkeypatch.setattr(deploy, "legacy_unit_properties", lambda: {"LoadState": "not-found"})
    commands = []
    monkeypatch.setattr(deploy.subprocess, "run", lambda command, **kwargs: commands.append(command))
    deploy.restore_legacy_unit(manifest, launcher)
    assert len(commands) == 1
    command = commands[0]
    assert command[:3] == ["systemd-run", "--user", "--unit=" + deploy.LEGACY_UNIT]
    assert "--property=Restart=on-failure" in command
    assert "--property=RestartSec=3s" in command
    assert "--property=WorkingDirectory=/reviewed/cwd" in command
    assert command[-3:] == ["--", "/reviewed/node", str(launcher)]
    monkeypatch.setattr(deploy, "legacy_unit_properties", lambda: {"LoadState": "loaded"})
    with pytest.raises(RuntimeError, match="refusing to replace"):
        deploy.restore_legacy_unit(manifest, launcher)
    assert len(commands) == 1


@pytest.fixture
def rollback(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    backup = runtime / "rollback/overview-preview.mjs"
    backup.parent.mkdir(parents=True)
    backup.write_text("// test launcher")
    manifest = {"backup": str(backup), "launcher_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                "original_command": [str(tmp_path / "bin/node"), "/tmp/old-preview.mjs"],
                "original_cwd": str(tmp_path / "launcher-cwd"),
                "preview_cwd": str(tmp_path / "old/frontend")}
    (backup.parent / "preview.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(deploy, "RUNTIME", runtime)
    ports = {9003: None, 5318: None}
    monkeypatch.setattr(deploy, "listening_pid", ports.get)
    monkeypatch.setattr(deploy, "systemctl", lambda *args, **kwargs: None)
    return manifest, ports


def test_rollback_waits_for_both_ports_owned_by_saved_launcher(monkeypatch, rollback, capsys):
    manifest, ports = rollback
    def spawn(*args, **kwargs):
        assert kwargs["cwd"] == manifest["original_cwd"]
        ports.update({9003: 301, 5318: 300})
        return SimpleNamespace(pid=300, poll=lambda: None)
    monkeypatch.setattr(deploy.subprocess, "Popen", spawn)
    monkeypatch.setattr(deploy, "process_identity", lambda pid: vite_identity(pid, Path(manifest["preview_cwd"]), parent=300))
    deploy.rollback_preview()
    assert "Restored saved" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["exited", "wrong_parent", "wrong_fixture"])
def test_rollback_cannot_report_success_for_an_unrelated_listener(monkeypatch, rollback, failure, capsys):
    manifest, ports = rollback
    def spawn(*args, **kwargs):
        ports.update({9003: 301, 5318: 999 if failure == "wrong_fixture" else 300})
        return SimpleNamespace(pid=300, poll=lambda: 1 if failure == "exited" else None)
    monkeypatch.setattr(deploy.subprocess, "Popen", spawn)
    monkeypatch.setattr(deploy, "process_identity", lambda pid: vite_identity(pid, Path(manifest["preview_cwd"]), parent=999))
    with pytest.raises(RuntimeError):
        deploy.rollback_preview()
    assert "Restored saved" not in capsys.readouterr().out


@pytest.fixture
def monitoring_installation(monkeypatch, tmp_path):
    root = tmp_path / 'checkout'
    runtime = root / '.runtime/server-monitoring-9003'
    (runtime / 'venv/bin').mkdir(parents=True)
    (runtime / 'venv/bin/python').touch()
    (runtime / 'state').mkdir(mode=0o700)
    (root / 'server_monitoring').mkdir()
    (root / 'server_monitoring/task_history.py').touch()
    (root / 'frontend/node_modules/vite/bin').mkdir(parents=True)
    (root / 'frontend/node_modules/vite/bin/vite.js').touch()
    config = {'enabled': True, 'state_dir': str(runtime / 'state'),
              'allowed_users': [{'username': name, 'user_id': f'test-user-{index}'}
                                for index, name in enumerate(deploy.EXPECTED_USERS)]}
    deploy.private_json(runtime / 'config.json', config)
    home = tmp_path / 'home'
    units = home / '.config/systemd/user'
    units.mkdir(parents=True)
    monkeypatch.setattr(deploy, 'ROOT', root)
    monkeypatch.setattr(deploy, 'RUNTIME', runtime)
    monkeypatch.setattr(deploy.Path, 'home', classmethod(lambda cls: home))
    calls = []
    monkeypatch.setattr(deploy, 'systemctl', lambda *args, **kwargs: calls.append((args, kwargs)))
    return root, runtime, units, calls


def test_prepare_task_history_changes_only_its_new_unit(monitoring_installation, monkeypatch):
    root, runtime, units, calls = monitoring_installation
    preserved = {runtime / 'config.json': (runtime / 'config.json').read_bytes()}
    for role in ('collector', 'api', 'preview'):
        file = units / deploy.UNITS[role]
        file.write_text(f'Existing reviewed {role} unit\n')
        preserved[file] = file.read_bytes()
    monkeypatch.setattr(deploy, 'compatible_node', lambda *args: pytest.fail('reader does not require Node'))
    monkeypatch.setattr(deploy.subprocess, 'run', lambda *args, **kwargs: pytest.fail('must not query Docker, auth or existing services'))

    deploy.prepare_task_history()

    assert all(file.read_bytes() == data for file, data in preserved.items())
    assert calls == [(('daemon-reload',), {})]
    content = (units / deploy.UNITS['task-history']).read_text()
    assert f'WorkingDirectory={root}' in content
    assert f'ExecStart="{runtime}/venv/bin/python" "-m" "server_monitoring.task_history" "--output-dir" "{runtime}/state"' in content
    assert 'MemoryMax=256M\nCPUQuota=20%\n' in content
    assert 'UMask=0077' in content and 'NoNewPrivileges=yes' in content
    assert '--once' not in content and 'Requires=' not in content and 'PartOf=' not in content


@pytest.mark.parametrize('problem', ['different_state', 'state_symlink', 'missing_module', 'unrelated_unit', 'unit_symlink'])
def test_prepare_task_history_refuses_unverified_destinations(monitoring_installation, problem):
    root, runtime, units, calls = monitoring_installation
    target = units / deploy.UNITS['task-history']
    if problem == 'different_state':
        deploy.private_json(runtime / 'config.json', {'state_dir': '/unrelated/state'})
    elif problem == 'state_symlink':
        original = runtime / 'state'
        original.rename(runtime / 'other-state')
        original.symlink_to(runtime / 'other-state', target_is_directory=True)
    elif problem == 'missing_module':
        (root / 'server_monitoring/task_history.py').unlink()
    elif problem == 'unrelated_unit':
        target.write_text('[Service]\nExecStart=/unrelated\n')
    else:
        other = units / 'unrelated.service'
        other.write_text('# Managed by scripts/server_monitoring_9003.py\n')
        target.symlink_to(other)
    with pytest.raises(RuntimeError):
        deploy.prepare_task_history()
    assert calls == []


def test_full_prepare_generates_all_four_independent_units(monitoring_installation, monkeypatch):
    root, runtime, units, calls = monitoring_installation
    users = [{'username': name, 'user_id': f'test-user-{index}', 'status': 'active', 'must_change_password': False}
             for index, name in enumerate(deploy.EXPECTED_USERS)]
    monkeypatch.setattr(deploy, 'compatible_node', lambda value: '/validated/node')
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(users))
    monkeypatch.setattr(deploy.subprocess, 'run', run)
    deploy.prepare(Path('/validated/node'))
    assert set(file.name for file in units.iterdir()) == set(deploy.UNITS.values())
    assert len(commands) == 1 and commands[0][:3] == ['docker', 'exec', '-i']
    assert calls == [(('daemon-reload',), {})]
    assert 'server_monitoring.task_history' in (units / deploy.UNITS['task-history']).read_text()
    assert 'server_monitoring.collector' in (units / deploy.UNITS['collector']).read_text()
    assert 'server_monitoring.api' in (units / deploy.UNITS['api']).read_text()
    assert 'DEV_MONITORING_PROXY_TARGET=http://127.0.0.1:5319' in (units / deploy.UNITS['preview']).read_text()


@pytest.mark.parametrize('action,expected,check', [
    ('task-history', ('start', deploy.UNITS['task-history']), True),
    ('start', ('start', deploy.UNITS['collector'], deploy.UNITS['task-history'], deploy.UNITS['api']), True),
    ('stop', ('stop', deploy.UNITS['preview'], deploy.UNITS['api'], deploy.UNITS['task-history'], deploy.UNITS['collector']), True),
    ('status', ('status', *deploy.UNITS.values(), '--no-pager'), False),
])
def test_service_actions_include_reader_without_unrelated_restarts(monkeypatch, action, expected, check):
    calls = []
    monkeypatch.setattr(deploy.sys, 'argv', ['server_monitoring_9003.py', action])
    monkeypatch.setattr(deploy, 'systemctl', lambda *args, **kwargs: calls.append((args, kwargs.get('check', True))))
    monkeypatch.setattr(deploy, 'prepare', lambda *args: pytest.fail('service actions must not rewrite config'))
    monkeypatch.setattr(deploy, 'prepare_task_history', lambda: pytest.fail('start must not reinstall units'))
    deploy.main()
    assert calls == [(expected, check)]


def test_cli_prepare_task_history_does_not_call_full_prepare(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy.sys, 'argv', ['server_monitoring_9003.py', 'prepare-task-history'])
    monkeypatch.setattr(deploy, 'prepare_task_history', lambda: calls.append('reader-only'))
    monkeypatch.setattr(deploy, 'prepare', lambda *args: pytest.fail('must not replace account configuration'))
    deploy.main()
    assert calls == ['reader-only']

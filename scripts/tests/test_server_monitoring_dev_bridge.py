from __future__ import annotations

import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from scripts import server_monitoring_dev_bridge as bridge


@pytest.fixture(autouse=True)
def forbid_real_commands(monkeypatch):
    monkeypatch.setattr(
        bridge.subprocess, "run",
        lambda *args, **kwargs: pytest.fail("Tests must not invoke real systemctl or other subprocesses"),
    )


@pytest.fixture
def environment(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    runtime_root = tmp_path / "run-user"
    runtime_root.mkdir(mode=0o700)
    proxy = tmp_path / "systemd-socket-proxyd"
    proxy.write_text("fake executable\n")
    proxy.chmod(0o700)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(bridge, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(bridge, "RUNTIME", runtime_root / "nexpoly-server-monitoring")
    monkeypatch.setattr(bridge, "SOCKET", bridge.RUNTIME / "api.sock")
    monkeypatch.setattr(bridge, "PROXY", proxy)
    calls = []

    def systemctl(*arguments, **kwargs):
        calls.append((arguments, kwargs))
        output = ""
        if arguments[0] == "show":
            target = home / ".config/systemd/user" / arguments[1]
            output = f"LoadState=loaded\nFragmentPath={target}\nDropInPaths=\n"
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(bridge, "systemctl", systemctl)
    return SimpleNamespace(home=home, calls=calls, systemctl=systemctl)


@pytest.fixture
def prepared(environment):
    bridge.prepare()
    environment.calls.clear()
    return environment


def patch_stat(monkeypatch, path, *, mode, uid=None):
    original = Path.lstat
    info = SimpleNamespace(st_mode=mode, st_uid=os.getuid() if uid is None else uid)
    monkeypatch.setattr(Path, "lstat", lambda self, *a, **kw: info if self == path else original(self, *a, **kw))


def test_socket_path_is_fixed_short_and_independent_of_workspace():
    assert bridge.RUNTIME_ROOT == Path(f"/run/user/{os.getuid()}")
    assert bridge.SOCKET == Path(f"/run/user/{os.getuid()}/nexpoly-server-monitoring/api.sock")
    assert len(os.fsencode(bridge.SOCKET)) < 108


def test_environment_uses_the_current_user_bus(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/untrusted/runtime")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/untrusted/bus")
    result = bridge.user_environment()
    assert result["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"
    assert result["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path=/run/user/{os.getuid()}/bus"


def test_systemctl_wrapper_is_user_scoped(monkeypatch):
    calls = []
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    bridge.systemctl("status", "unit", check=False, capture_output=True)
    arguments, options = calls[0]
    assert arguments == (["systemctl", "--user", "status", "unit"],)
    assert options["check"] is False and options["capture_output"] is True
    assert options["env"] == bridge.user_environment()


def test_quoted_escapes_systemd_expansion():
    assert bridge.quoted('a\\b"%$') == '"a\\\\b\\"%%$$"'


@pytest.mark.parametrize("character", ["\n", "\r", "\0"])
def test_quoted_rejects_control_characters(character):
    with pytest.raises(ValueError):
        bridge.quoted("bad" + character + "argument")


def test_units_only_listen_on_unix_and_proxy_fixed_loopback():
    socket_unit = bridge.bridge_unit("socket")
    service_unit = bridge.bridge_unit("service")
    assert f"ListenStream={bridge.SOCKET}\n" in socket_unit
    assert socket_unit.count("ListenStream=") == 1
    assert "SocketMode=0600\nDirectoryMode=0700\nRemoveOnStop=yes\n" in socket_unit
    assert 'ExecStart="/usr/lib/systemd/systemd-socket-proxyd" 127.0.0.1:5319\n' in service_unit
    assert f"Requires={bridge.UNITS['socket']}\n" in service_unit
    for content in (socket_unit, service_unit):
        assert f"After={bridge.API_UNIT}\n" in content
        assert f"Requisite={bridge.API_UNIT}" not in content
        assert f"Requires={bridge.API_UNIT}" not in content
        assert f"Wants={bridge.API_UNIT}" not in content
        assert f"BindsTo={bridge.API_UNIT}" not in content
        assert f"PartOf={bridge.API_UNIT}" not in content
        assert "[Install]" not in content and "WantedBy=" not in content


def test_prepare_creates_only_private_files_and_reloads(environment):
    bridge.prepare()
    assert stat.S_IMODE(bridge.RUNTIME.stat().st_mode) == 0o700
    assert not bridge.SOCKET.exists()
    directory = environment.home / ".config/systemd/user"
    assert {path.name for path in directory.iterdir()} == set(bridge.UNITS.values())
    for role, name in bridge.UNITS.items():
        assert (directory / name).read_text() == bridge.bridge_unit(role)
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert environment.calls == [(("daemon-reload",), {})]
    bridge.prepare()
    assert environment.calls == [(("daemon-reload",), {}), (("daemon-reload",), {})]


@pytest.mark.parametrize("role", ["socket", "service"])
def test_prepare_refuses_unrelated_unit_before_writing_either(environment, role):
    directory = bridge.unit_directory(create=True)
    target = directory / bridge.UNITS[role]
    target.write_text("# Someone else's unit\n")
    target.chmod(0o600)
    with pytest.raises(RuntimeError, match="unrelated unit"):
        bridge.prepare()
    assert target.read_text() == "# Someone else's unit\n"
    assert list(directory.iterdir()) == [target]
    assert not bridge.RUNTIME.exists()
    assert environment.calls == []


@pytest.mark.parametrize("kind", ["symlink", "directory", "world_writable", "foreign_owner"])
def test_prepare_refuses_unsafe_units(environment, monkeypatch, tmp_path, kind):
    target = bridge.unit_directory(create=True) / bridge.UNITS["socket"]
    if kind == "symlink":
        target.symlink_to(tmp_path / "missing")
    elif kind == "directory":
        target.mkdir()
    else:
        target.write_text(bridge.MARKER + "\n")
        if kind == "world_writable":
            target.chmod(0o666)
        else:
            patch_stat(monkeypatch, target, mode=stat.S_IFREG | 0o600, uid=os.getuid() + 1)
    with pytest.raises(RuntimeError, match="unsafe unit"):
        bridge.prepare()
    assert environment.calls == []


@pytest.mark.parametrize("target", ["runtime_root", "runtime", "unit_parent"])
def test_symlinked_directories_are_rejected(environment, tmp_path, target):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    if target == "runtime_root":
        bridge.RUNTIME_ROOT.rmdir()
        bridge.RUNTIME_ROOT.symlink_to(elsewhere, target_is_directory=True)
    elif target == "runtime":
        bridge.RUNTIME.symlink_to(elsewhere, target_is_directory=True)
    else:
        (environment.home / ".config").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(RuntimeError, match="not a symlink|Unsafe directory ancestor"):
        bridge.prepare()
    assert list(elsewhere.iterdir()) == []
    assert environment.calls == []


@pytest.mark.parametrize("location", ["runtime_root", "runtime"])
@pytest.mark.parametrize("kind", ["permissions", "foreign_owner"])
def test_runtime_directories_require_current_owner_and_0700(environment, monkeypatch, location, kind):
    bridge.RUNTIME.mkdir(mode=0o700)
    target = bridge.RUNTIME_ROOT if location == "runtime_root" else bridge.RUNTIME
    if kind == "permissions":
        target.chmod(0o755)
    else:
        patch_stat(monkeypatch, target, mode=stat.S_IFDIR | 0o700, uid=os.getuid() + 1)
    with pytest.raises(RuntimeError):
        bridge.prepare()
    assert environment.calls == []


@pytest.mark.parametrize("kind", ["file", "symlink", "foreign_owner", "permissions"])
def test_runtime_socket_cannot_be_replaced_or_exposed(environment, monkeypatch, tmp_path, kind):
    bridge.RUNTIME.mkdir(mode=0o700)
    if kind == "file":
        bridge.SOCKET.touch(mode=0o600)
    elif kind == "symlink":
        bridge.SOCKET.symlink_to(tmp_path / "missing")
    elif kind == "foreign_owner":
        patch_stat(monkeypatch, bridge.SOCKET, mode=stat.S_IFSOCK | 0o600, uid=os.getuid() + 1)
    else:
        patch_stat(monkeypatch, bridge.SOCKET, mode=stat.S_IFSOCK | 0o666)
    with pytest.raises(RuntimeError, match="Socket must"):
        bridge.prepare()
    assert environment.calls == []


def test_prepare_requires_existing_proxy_executable(environment):
    bridge.PROXY.chmod(0o600)
    with pytest.raises(RuntimeError, match="executable is missing"):
        bridge.prepare()
    assert environment.calls == []


def test_start_requires_api_active_without_starting_anything(prepared, monkeypatch):
    def systemctl(*arguments, **kwargs):
        result = prepared.systemctl(*arguments, **kwargs)
        if arguments[0] == "is-active":
            result.returncode = 3
        return result
    monkeypatch.setattr(bridge, "systemctl", systemctl)
    with pytest.raises(RuntimeError, match="must already be active"):
        bridge.start()
    assert [args[0] for args, _ in prepared.calls] == ["show", "show", "is-active"]
    assert prepared.calls[-1] == (("is-active", "--quiet", bridge.API_UNIT), {"check": False})


def test_start_only_starts_bridge_socket_and_verifies_0600(prepared, monkeypatch):
    original_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == bridge.SOCKET and any(call[0][0] == "start" for call in prepared.calls):
            return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=os.getuid())
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    bridge.start()
    assert prepared.calls[-1] == (("start", bridge.UNITS["socket"]), {})
    assert [args[0] for args, _ in prepared.calls] == ["show", "show", "is-active", "start"]


def test_start_does_not_report_success_without_socket(prepared, capsys):
    capsys.readouterr()
    with pytest.raises(RuntimeError, match="did not create its socket"):
        bridge.start()
    assert "Bridge listening" not in capsys.readouterr().out


@pytest.mark.parametrize("change", ["fragment", "drop_in", "unloaded"])
def test_start_and_stop_refuse_systemd_overrides(prepared, monkeypatch, change):
    def systemctl(*arguments, **kwargs):
        result = prepared.systemctl(*arguments, **kwargs)
        if change == "fragment":
            result.stdout = result.stdout.replace("FragmentPath=", "FragmentPath=/unrelated")
        elif change == "drop_in":
            result.stdout = result.stdout.replace("DropInPaths=", "DropInPaths=/unrelated.conf")
        else:
            result.stdout = result.stdout.replace("LoadState=loaded", "LoadState=not-found")
        return result
    monkeypatch.setattr(bridge, "systemctl", systemctl)
    for action in (bridge.start, bridge.stop):
        with pytest.raises(RuntimeError, match="overridden or unloaded"):
            action()
    assert all(args[0] == "show" for args, _ in prepared.calls)


def test_start_requires_exact_prepared_configuration(prepared):
    target = bridge.unit_directory() / bridge.UNITS["service"]
    target.write_text(target.read_text().replace("127.0.0.1:5319", "0.0.0.0:9999"))
    with pytest.raises(RuntimeError, match="differs from its expected configuration"):
        bridge.start()
    assert all(args[0] == "show" for args, _ in prepared.calls)


def test_stop_only_stops_bridge_units(prepared):
    bridge.stop()
    assert prepared.calls[-1] == (("stop", bridge.UNITS["socket"], bridge.UNITS["service"]), {})
    assert all(bridge.API_UNIT not in args for args, _ in prepared.calls)


def test_status_does_not_change_any_units(environment, capsys):
    bridge.status()
    assert environment.calls == [
        (("status", "--no-pager", bridge.UNITS["socket"], bridge.UNITS["service"]), {"check": False}),
    ]
    assert str(bridge.SOCKET) in capsys.readouterr().out
    assert not bridge.RUNTIME.exists()

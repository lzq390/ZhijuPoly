#!/usr/bin/env python3
"""Manage only the dedicated 9003 monitoring preview and its read-only services."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime/server-monitoring-9003"
UNITS = {
    "collector": "nexpoly-server-monitoring-collector.service",
    "task-history": "nexpoly-server-monitoring-task-history.service",
    "api": "nexpoly-server-monitoring-api.service",
    "preview": "nexpoly-server-monitoring-preview.service",
}
EXPECTED_USERS = ("zhiju000", "zhiju001", "zhiju002")
LEGACY_LAUNCHER = Path("/tmp/nexpoly-overview-preview.mjs")
LEGACY_UNIT = "nexpoly-overview-preview-9003.service"


def private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def user_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    environment["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
    return environment


def systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *arguments], env=user_environment(), check=check)


def quoted(value: Path | str) -> str:
    value = str(value)
    if any(character in value for character in "\n\r\0"):
        raise ValueError("Invalid systemd argument")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def compatible_node(explicit: Path | None = None) -> str:
    candidate = str(explicit) if explicit is not None else shutil.which("node")
    if candidate is None or not Path(candidate).is_absolute():
        raise RuntimeError("Specify an absolute compatible Node path with prepare --node")
    node = str(Path(candidate).resolve(strict=True))
    result = subprocess.run([node, "--version"], capture_output=True, text=True, check=True, timeout=5)
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\s*", result.stdout)
    if match is None:
        raise RuntimeError("Cannot determine the selected Node version")
    version = tuple(map(int, match.groups()))
    if not ((20, 19, 0) <= version < (21, 0, 0) or version >= (22, 12, 0)):
        raise RuntimeError("Vite requires Node 20.19+ on 20.x or Node 22.12+; select prepare --node /absolute/node")
    return node


def prepare(node_path: Path | None = None) -> None:
    python = RUNTIME / "venv/bin/python"
    if not python.is_file():
        raise RuntimeError("Install the monitoring virtual environment first")
    node = compatible_node(node_path)
    if not (ROOT / "frontend/node_modules/vite/bin/vite.js").is_file():
        raise RuntimeError("Install frontend dependencies first")
    query = """
import json, os, psycopg
from psycopg.rows import dict_row
with psycopg.connect(os.environ['AUTH_POSTGRES_DSN'], row_factory=dict_row, connect_timeout=3) as connection:
    connection.execute('SET TRANSACTION READ ONLY')
    rows=connection.execute('SELECT user_id,username,status,must_change_password FROM auth.users WHERE username = ANY(%s) ORDER BY username', (['zhiju000','zhiju001','zhiju002'],)).fetchall()
    print(json.dumps(rows, default=str))
"""
    result = subprocess.run(["docker", "exec", "-i", "nexpoly_dev-backend-1", "python", "-"],
                            input=query, text=True, capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError("Cannot resolve approved users from the development authentication database")
    users = json.loads(result.stdout)
    if tuple(item["username"] for item in users) != EXPECTED_USERS or any(
        item["status"] != "active" or item["must_change_password"] for item in users
    ):
        raise RuntimeError("All three approved development accounts must exist, be active and ready")
    state = RUNTIME / "state"
    for directory in (RUNTIME, state, RUNTIME / "artifacts"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    private_json(RUNTIME / "config.json", {
        "enabled": True, "state_dir": str(state),
        "allowed_users": [{"username": item["username"], "user_id": item["user_id"]} for item in users],
    })
    for role in UNITS:
        write_unit(role, monitoring_unit(role, node=node))
    systemctl("daemon-reload")
    print("Prepared private account bindings and four independent user units; nothing started.")


def monitoring_unit(role: str, *, node: str | None = None) -> str:
    python = RUNTIME / "venv/bin/python"
    state = RUNTIME / "state"
    common = "\n".join([
        "# Managed by scripts/server_monitoring_9003.py",
        "[Unit]", "Description=NexPoly dedicated 9003 monitoring {role}",
        "[Service]", "Type=simple", "UMask=0077", "NoNewPrivileges=yes",
        "Restart=on-failure", "RestartSec=3", "TimeoutStopSec=10",
        "KillMode=control-group", "Nice=10", "Environment=PYTHONUNBUFFERED=1",
        "Environment=PYTHONDONTWRITEBYTECODE=1", "StandardOutput=journal", "StandardError=journal",
    ])
    commands = {
        "collector": (ROOT, [python, "-m", "server_monitoring.collector", "--output-dir", state],
                      "MemoryMax=192M\nCPUQuota=20%\n"),
        "task-history": (ROOT, [python, "-m", "server_monitoring.task_history", "--output-dir", state],
                         "MemoryMax=256M\nCPUQuota=20%\n"),
        "api": (ROOT, [python, "-m", "server_monitoring.api", "--config", RUNTIME / "config.json"],
                "MemoryMax=256M\nCPUQuota=50%\n"),
        "preview": (ROOT / "frontend", [node, ROOT / "frontend/node_modules/vite/bin/vite.js",
                                           "--host", "0.0.0.0", "--port", "9003", "--strictPort"],
                    "MemoryMax=2G\nEnvironment=DEV_PROXY_TARGET=http://127.0.0.1:18000\n"
                    "Environment=DEV_MONITORING_PROXY_TARGET=http://127.0.0.1:5319\n"
                    "Environment=VITE_SERVER_MONITORING_ENABLED=true\nEnvironment=VITE_AGENT_WORKSPACE_URL=\n"),
    }
    if role == "preview" and node is None:
        raise ValueError("Preview unit requires an explicitly validated Node executable")
    working, command, extra = commands[role]
    return (common.format(role=role) + "\nWorkingDirectory=" + str(working).replace("%", "%%")
            + "\nExecStart=" + " ".join(quoted(item) for item in command) + "\n" + extra)


def write_unit(role: str, content: str) -> None:
    unit_root = Path.home() / ".config/systemd/user"
    unit_root.mkdir(parents=True, exist_ok=True)
    target = unit_root / UNITS[role]
    if target.is_symlink() or (target.exists() and not target.read_text().startswith("# Managed by scripts/server_monitoring_9003.py")):
        raise RuntimeError(f"Refusing to replace unrelated unit {target.name}")
    target.write_text(content)


def prepare_task_history() -> None:
    """Install just the new reader, retaining live account and service configuration."""
    if not (RUNTIME / "venv/bin/python").is_file():
        raise RuntimeError("Install the monitoring virtual environment first")
    if not (ROOT / "server_monitoring/task_history.py").is_file():
        raise RuntimeError("The task history reader must be installed before preparing its unit")
    state = RUNTIME / "state"
    config = json.loads((RUNTIME / "config.json").read_text())
    if config.get("state_dir") != str(state) or not state.is_dir() or state.is_symlink():
        raise RuntimeError("Task history must use the existing monitoring state directory")
    write_unit("task-history", monitoring_unit("task-history"))
    systemctl("daemon-reload")
    print("Prepared only the task history user unit; existing services and account bindings unchanged, nothing started.")


def listening_pid(port: int) -> int | None:
    output = subprocess.check_output(["ss", "-ltnp", f"sport = :{port}"], text=True)
    matches = set(re.findall(r"pid=(\d+)", output))
    if len(matches) > 1:
        raise RuntimeError(f"Multiple processes are listening on {port}")
    if not matches and any(line.lstrip().startswith("LISTEN") for line in output.splitlines()):
        raise RuntimeError(f"Cannot identify the process listening on {port}")
    return int(next(iter(matches))) if matches else None


def process_identity(pid: int) -> dict:
    """Bind signals to one exact same-user process, including its start time."""
    root = Path(f"/proc/{pid}")
    before = (root / "stat").read_text()
    fields = before[before.rfind(")") + 2:].split()
    status = (root / "status").read_text()
    owners = re.search(r"^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", status, re.M)
    if owners is None or any(int(value) != os.getuid() for value in owners.groups()):
        raise RuntimeError("Preview process is not owned by the current user")
    identity = {
        "pid": pid, "parent": int(fields[1]), "start_ticks": int(fields[19]),
        "arguments": [part.decode() for part in (root / "cmdline").read_bytes().split(b"\0") if part],
        "cwd": str((root / "cwd").resolve(strict=True)),
        "executable": str((root / "exe").resolve(strict=True)),
        "cgroup": (root / "cgroup").read_text(),
    }
    after = (root / "stat").read_text()
    if int(after[after.rfind(")") + 2:].split()[19]) != identity["start_ticks"]:
        raise RuntimeError("Preview process identity changed while inspecting it")
    return identity


def require_vite(identity: dict, cwd: Path) -> None:
    arguments = identity["arguments"]
    if (len(arguments) != 7 or identity["cwd"] != str(cwd)
            or Path(identity["executable"]).name != "node"
            or Path(arguments[0]).resolve() != Path(identity["executable"])
            or (cwd / arguments[1]).resolve() != (cwd / "node_modules/vite/bin/vite.js").resolve()
            or arguments[2:] != ["--host", "0.0.0.0", "--port", "9003", "--strictPort"]):
        raise RuntimeError("9003 is not the expected Vite preview; leaving it untouched")


def unit_preview_pid() -> int:
    result = subprocess.check_output(
        ["systemctl", "--user", "show", UNITS["preview"], "--property=MainPID", "--value"],
        env=user_environment(), text=True,
    ).strip()
    if not result.isdecimal():
        raise RuntimeError("Cannot identify the managed preview process")
    return int(result)


def legacy_unit_properties() -> dict[str, str]:
    properties = ("Id,LoadState,ActiveState,MainPID,InvocationID,ControlGroup,WorkingDirectory,"
                  "Restart,RestartUSec,FragmentPath,Transient,DropInPaths,Description,Type")
    result = subprocess.check_output(
        ["systemctl", "--user", "show", LEGACY_UNIT, "--property=" + properties],
        env=user_environment(), text=True,
    )
    return dict(line.split("=", 1) for line in result.splitlines() if "=" in line)


def capture_legacy_unit(parent: dict, child: dict) -> dict | None:
    """Recognize the one prior preview unit, never infer a unit to stop from a PID."""
    group = f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/{LEGACY_UNIT}"
    membership = f"0::{group}\n"
    parent_group, child_group = parent.get("cgroup", ""), child.get("cgroup", "")
    if parent_group != membership:
        if ".service" in parent_group:
            raise RuntimeError("Preview belongs to an unrecognized service; leaving it untouched")
        return None
    properties = legacy_unit_properties()
    fragment = Path(f"/run/user/{os.getuid()}/systemd/transient/{LEGACY_UNIT}")
    expected = {"Id": LEGACY_UNIT, "LoadState": "loaded", "ActiveState": "active",
                "MainPID": str(parent["pid"]), "ControlGroup": group,
                "WorkingDirectory": parent["cwd"], "Transient": "yes",
                "FragmentPath": str(fragment), "DropInPaths": "", "Type": "simple"}
    if (child_group != membership or any(properties.get(key) != value for key, value in expected.items())
            or not re.fullmatch(r"[0-9a-f]{32}", properties.get("InvocationID", ""))):
        raise RuntimeError("Cannot bind the previous preview unit to its exact processes")
    content = fragment.read_text()
    if len(content) > 32 * 1024:
        raise RuntimeError("Previous preview unit is unexpectedly large")
    values: dict[str, list[str]] = {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in {"Description", "Type", "Restart", "RestartSec", "WorkingDirectory", "ExecStart"}:
            raise RuntimeError("Previous preview unit has unsupported configuration; leaving it untouched")
        values.setdefault(key, []).append(value)
    commands = [value for value in values.get("ExecStart", []) if value]
    if (len(commands) != 1 or shlex.split(commands[0]) != parent["arguments"]
            or values.get("Type", ["simple"]) != ["simple"]
            or values.get("WorkingDirectory") != [parent["cwd"]]
            or values.get("Restart") != ["on-failure"]
            or values.get("RestartSec") != ["3s"]
            or properties.get("Restart") != "on-failure" or properties.get("RestartUSec") != "3s"):
        raise RuntimeError("Previous preview unit differs from the supported startup contract")
    if legacy_unit_properties() != properties:
        raise RuntimeError("Previous preview unit changed during capture")
    return {"properties": properties, "content": content}


def stop_legacy_unit(captured: dict, parent: dict, child: dict) -> None:
    if (legacy_unit_properties() != captured["properties"]
            or process_identity(parent["pid"]) != parent
            or process_identity(child["pid"]) != child):
        raise RuntimeError("Previous preview unit changed; no service stopped")
    # Stopping the verified unit suppresses Restart=on-failure for the old launcher.
    systemctl("stop", LEGACY_UNIT)


def restore_legacy_unit(manifest: dict, launcher: Path) -> None:
    record = manifest["legacy_unit"]
    content = Path(record["backup"]).read_bytes()
    if record["name"] != LEGACY_UNIT or hashlib.sha256(content).hexdigest() != record["sha256"]:
        raise RuntimeError("Saved legacy unit changed")
    properties = legacy_unit_properties()
    if properties.get("LoadState") != "not-found":
        raise RuntimeError("The prior unit name is still loaded; refusing to replace it")
    command = ["systemd-run", "--user", "--unit=" + LEGACY_UNIT, "--service-type=simple",
               "--description=" + record["description"], "--property=Restart=on-failure",
               "--property=RestartSec=3s", "--property=WorkingDirectory=" + manifest["original_cwd"],
               "--", manifest["original_command"][0], str(launcher)]
    subprocess.run(command, env=user_environment(), check=True, timeout=15)


def terminate_exact_process(identity: dict) -> None:
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("Safe preview takeover requires Linux pidfd support")
    descriptor = os.pidfd_open(identity["pid"])
    try:
        if process_identity(identity["pid"]) != identity:
            raise RuntimeError("Preview process changed; no signal sent")
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    finally:
        os.close(descriptor)


def wait_managed_preview() -> None:
    deadline = time.monotonic() + 20
    while True:
        pid = listening_pid(9003)
        if pid is not None:
            if pid != unit_preview_pid():
                raise RuntimeError("An unrelated process acquired 9003; preview is not ready")
            require_vite(process_identity(pid), ROOT / "frontend")
            return
        if time.monotonic() > deadline:
            raise RuntimeError("Preview did not bind port 9003; run rollback-preview")
        time.sleep(0.2)


def preview() -> None:
    with socket.create_connection(("127.0.0.1", 5319), timeout=3):
        pass
    pid = listening_pid(9003)
    if pid:
        if unit_preview_pid() == pid:
            require_vite(process_identity(pid), ROOT / "frontend")
            print("Monitoring preview already owns 9003.")
            return
        child = process_identity(pid)
        original = process_identity(child["parent"])
        arguments = original["arguments"]
        # The launcher can pass an explicit child cwd; its own cwd is independent.
        preview_cwd = Path(child["cwd"])
        require_vite(child, preview_cwd)
        backup = RUNTIME / "rollback/overview-preview.mjs"
        source = Path(arguments[1]) if len(arguments) == 2 else None
        if (source is None or source not in {LEGACY_LAUNCHER, backup} or not source.is_file()
                or Path(original["executable"]).name != "node"
                or Path(arguments[0]).resolve() != Path(original["executable"])
                or listening_pid(5318) != original["pid"]):
            raise RuntimeError("9003 is owned by an unrecognized process; leaving it untouched")
        if source == backup:
            previous = json.loads((RUNTIME / "rollback/preview.json").read_text())
            if hashlib.sha256(backup.read_bytes()).hexdigest() != previous["launcher_sha256"]:
                raise RuntimeError("Saved preview launcher changed")
        legacy_unit = capture_legacy_unit(original, child)
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source != backup:
            shutil.copy2(source, backup)
        manifest = {
            "original_command": arguments, "original_cwd": original["cwd"],
            "preview_cwd": str(preview_cwd),
            "original_start_ticks": original["start_ticks"],
            "launcher_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(), "backup": str(backup),
        }
        if legacy_unit is not None:
            unit_backup = backup.parent / "legacy-preview.service"
            unit_backup.write_text(legacy_unit["content"])
            unit_backup.chmod(0o600)
            manifest["legacy_unit"] = {
                "name": LEGACY_UNIT, "backup": str(unit_backup),
                "sha256": hashlib.sha256(unit_backup.read_bytes()).hexdigest(),
                "description": legacy_unit["properties"]["Description"],
                "properties": legacy_unit["properties"],
            }
        private_json(RUNTIME / "rollback/preview.json", manifest)
        if process_identity(pid) != child:
            raise RuntimeError("Previous Vite process changed; no signal sent")
        if legacy_unit is not None:
            stop_legacy_unit(legacy_unit, original, child)
        else:
            terminate_exact_process(original)
        deadline = time.monotonic() + 10
        while listening_pid(9003) is not None or listening_pid(5318) is not None:
            if time.monotonic() > deadline:
                raise RuntimeError("Previous preview has not stopped; no new preview started")
            time.sleep(0.2)
    try:
        systemctl("start", UNITS["preview"])
        wait_managed_preview()
    except Exception:
        print("Preview failed to start; restore with rollback-preview.", file=sys.stderr)
        raise
    print("9003 now serves the current worktree with real development authentication.")


def rollback_preview() -> None:
    manifest = json.loads((RUNTIME / "rollback/preview.json").read_text())
    backup = Path(manifest["backup"])
    if hashlib.sha256(backup.read_bytes()).hexdigest() != manifest["launcher_sha256"]:
        raise RuntimeError("Saved preview launcher changed")
    systemctl("stop", UNITS["preview"])
    if listening_pid(9003) is not None or listening_pid(5318) is not None:
        raise RuntimeError("An unrelated process owns the previous preview ports")
    managed = "legacy_unit" in manifest
    process = None
    if managed:
        restore_legacy_unit(manifest, backup)
    else:
        log = os.open(RUNTIME / "rollback/preview.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            process = subprocess.Popen([manifest["original_command"][0], str(backup)],
                                       cwd=manifest["original_cwd"], stdout=log, stderr=log,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
        finally:
            os.close(log)
    deadline = time.monotonic() + 20
    expected_pid = None
    while True:
        if process is not None and process.poll() is not None:
            raise RuntimeError("Saved preview launcher exited before readiness")
        if managed:
            properties = legacy_unit_properties()
            if properties.get("ActiveState") == "failed":
                raise RuntimeError("Saved preview unit failed before readiness")
            current_pid = int(properties.get("MainPID", "0"))
            expected_pid = current_pid or None
        else:
            expected_pid = process.pid
        parent = listening_pid(5318)
        child = listening_pid(9003)
        if parent is not None and parent != expected_pid:
            raise RuntimeError("An unrelated process acquired the rollback API port")
        if child is not None:
            identity = process_identity(child)
            if identity["parent"] != expected_pid or parent != expected_pid:
                raise RuntimeError("An unrelated process acquired the rollback preview port")
            require_vite(identity, Path(manifest.get("preview_cwd", manifest["original_cwd"])))
            if managed:
                restored = process_identity(expected_pid)
                if (restored["arguments"] != [manifest["original_command"][0], str(backup)]
                        or restored["cwd"] != manifest["original_cwd"]
                        or capture_legacy_unit(restored, identity) is None):
                    raise RuntimeError("Restored preview unit identity differs from the saved launcher")
            break
        if time.monotonic() > deadline:
            raise RuntimeError("Saved preview launcher did not become ready")
        time.sleep(0.2)
    print(f"Restored saved frontend-only preview launcher (PID {expected_pid}, systemd={managed}).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "prepare-task-history", "task-history", "start", "stop", "status", "preview", "rollback-preview"))
    parser.add_argument("--node", type=Path, help="Absolute Vite-compatible Node executable for prepare")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.node)
    elif args.action == "prepare-task-history":
        prepare_task_history()
    elif args.action == "task-history":
        systemctl("start", UNITS["task-history"])
    elif args.action == "start":
        systemctl("start", UNITS["collector"], UNITS["task-history"], UNITS["api"])
    elif args.action == "stop":
        systemctl("stop", UNITS["preview"], UNITS["api"], UNITS["task-history"], UNITS["collector"])
    elif args.action == "status":
        systemctl("status", *UNITS.values(), "--no-pager", check=False)
    elif args.action == "preview":
        preview()
    else:
        rollback_preview()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc)) from None

#!/usr/bin/env python3
"""Single-use guest bootstrap. All privileged credentials stay on the host."""
from __future__ import annotations

import json
import errno
import os
from pathlib import Path
import pwd
import re
import select
import subprocess
import time

BOOTSTRAP = Path("/dev/virtio-ports/org.nexpoly.runner.bootstrap")
EVENTS = Path("/dev/virtio-ports/org.nexpoly.runner.events")
RUNTIME = Path("/run/nexpoly-ci-runner")
RUNNER = Path("/opt/actions-runner")
PROFILE = "nexpoly-ci-ephemeral-v1"
PROXY = "http://10.0.2.2:27892"
NO_PROXY = "localhost,127.0.0.1,::1,postgres,backend,lab-postgres"
MAX_PAYLOAD = 8192


def emit(event: str, **fields: object) -> None:
    """Events contain only fixed lifecycle fields, never command output."""
    payload = (json.dumps({"event": event, **fields}, separators=(",", ":")) + "\n").encode()
    if len(payload) > 2048:
        raise ValueError("event too large")
    try:
        fd = os.open(EVENTS, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    except OSError:
        pass  # The host can still reconcile the VM and runner through the API.


def validate_payload(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"repo_url", "runner_name", "registration_token", "labels"}:
        raise ValueError("invalid bootstrap fields")
    if value["repo_url"] != "https://github.com/lzq390/ZhijuPoly":
        raise ValueError("invalid repository")
    if not isinstance(value["runner_name"], str) or not re.fullmatch(r"nexpoly-ci-[a-zA-Z0-9-]{1,80}", value["runner_name"]):
        raise ValueError("invalid runner name")
    if value["labels"] != ["nexpoly-ci-ephemeral"]:
        raise ValueError("invalid labels")
    if not isinstance(value["registration_token"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", value["registration_token"]):
        raise ValueError("invalid registration credential")
    return value


def read_payload(timeout: float = 600) -> dict:
    deadline = time.monotonic() + timeout
    while not BOOTSTRAP.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("bootstrap port unavailable")
        time.sleep(0.2)
    fd = os.open(BOOTSTRAP, os.O_RDONLY | os.O_NONBLOCK)
    data = bytearray()
    next_ready = 0.0
    try:
        while time.monotonic() < deadline:
            if time.monotonic() >= next_ready:
                # Announce only after the receiving port is open. Repeat because
                # the host may attach its events socket after the first message.
                emit("ready")
                next_ready = time.monotonic() + 2
            if not select.select([fd], [], [], min(1, max(0, deadline - time.monotonic())))[0]:
                continue
            try:
                chunk = os.read(fd, 1024)
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOTCONN):
                    raise
                time.sleep(0.1)
                continue
            if not chunk:
                time.sleep(0.1)
                continue
            data.extend(chunk)
            if len(data) > MAX_PAYLOAD:
                raise ValueError("bootstrap payload too large")
            if b"\n" in data:
                line, trailing = data.split(b"\n", 1)
                if trailing.strip():
                    raise ValueError("multiple bootstrap payloads")
                return validate_payload(json.loads(line))
        raise TimeoutError("bootstrap timeout")
    finally:
        data[:] = b"\x00" * len(data)
        os.close(fd)


def runner_environment() -> dict[str, str]:
    return {
        "HOME": "/home/runner", "USER": "runner", "LOGNAME": "runner",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8", "ImageOS": "ubuntu24",
        "http_proxy": PROXY, "https_proxy": PROXY,
        "HTTP_PROXY": PROXY, "HTTPS_PROXY": PROXY,
        "no_proxy": NO_PROXY, "NO_PROXY": NO_PROXY,
        "AGENT_TOOLSDIRECTORY": "/opt/hostedtoolcache",
        "ACTIONS_RUNNER_HOOK_JOB_STARTED": "/usr/local/lib/nexpoly-ci-runner/job-started.sh",
        "ACTIONS_RUNNER_HOOK_JOB_COMPLETED": "/usr/local/lib/nexpoly-ci-runner/job-completed.sh",
    }


def drop_privileges() -> None:
    account = pwd.getpwnam("runner")
    os.initgroups("runner", account.pw_gid)
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)


def register() -> None:
    """Only the short-lived registration child ever receives the host token."""
    payload = read_payload()
    env = runner_environment()
    # Runner.Listener consumes and removes ACTIONS_RUNNER_INPUT_* itself.
    # Avoid putting the token in argv, seed data, the journal or a disk file.
    env["ACTIONS_RUNNER_INPUT_TOKEN"] = payload.pop("registration_token")
    try:
        completed = subprocess.run(
            [str(RUNNER / "config.sh"), "--unattended", "--ephemeral", "--disableupdate",
             "--url", payload["repo_url"], "--name", payload["runner_name"],
             "--labels", ",".join(payload["labels"]), "--work", "_work"],
            cwd=RUNNER, env=env, preexec_fn=drop_privileges,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180,
            check=False,
        )
    finally:
        env.pop("ACTIONS_RUNNER_INPUT_TOKEN", None)
        payload.clear()
        # Registration diagnostics are not needed by jobs and might contain
        # credential material transformed by upstream logging.
        import shutil
        shutil.rmtree(RUNNER / "_diag", ignore_errors=True)
    if completed.returncode:
        raise RuntimeError("runner registration failed")


def isolated_registration() -> None:
    # Clearing Python strings cannot wipe every copy. Fork before reading any
    # token, then destroy the child's address space before running untrusted CI.
    child = os.fork()
    if child == 0:
        status = 1
        try:
            register()
            status = 0
        except BaseException:
            pass  # Never serialize or log credential-bearing exception text.
        finally:
            os._exit(status)
    _, status = os.waitpid(child, 0)
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise RuntimeError("runner registration failed")


def main() -> int:
    try:
        if Path("/etc/nexpoly-ci-runner/profile").read_text().strip() != PROFILE:
            raise ValueError("unrecognized runner profile")
        RUNTIME.mkdir(mode=0o700, exist_ok=True)
        isolated_registration()
        env = runner_environment()
        emit("registered")
        result = subprocess.run([str(RUNNER / "run.sh")], cwd=RUNNER, env=env,
                                preexec_fn=drop_privileges, check=False)
        emit("runner_exit", exit_code=result.returncode)
        return result.returncode
    except Exception:
        # Do not stringify exceptions: malformed JSON and subprocess errors may
        # contain a registration credential.
        emit("failed", reason="guest_bootstrap_failed")
        return 1
    finally:
        subprocess.run(["systemctl", "poweroff", "--no-block"], check=False)


if __name__ == "__main__":
    raise SystemExit(main())

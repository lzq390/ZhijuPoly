#!/usr/bin/env python3
"""Apply the reviewed 27891 -> 17891 runner proxy migration while quiescent.

This command never stops a VM/controller, changes a shared proxy, reads an App
key, calls a GitHub mutation, or resumes runner replenishment. An administrator
must first drain and retire the registered standby VM through runnerctl.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE))
import host

OLD_PORT = 27891
NEW_PORT = 17891
BACKUP_NAME = "config.toml.before-upstream-17891"


def migrated_config(content: bytes) -> bytes:
    """Preserve the installed file except for one network port integer."""
    try:
        text = content.decode("utf-8")
        original = tomllib.loads(text)
        port = original["network"]["upstream_port"]
    except (UnicodeError, ValueError, KeyError, TypeError) as error:
        raise host.HostError("Cannot parse installed runner configuration") from error
    if type(port) is not int or port not in {OLD_PORT, NEW_PORT}:
        raise host.HostError("Migration accepts only the reviewed old or new upstream port")
    if port == NEW_PORT:
        return content
    section = re.search(r"(?m)^\[network\][ \t]*(?:#[^\r\n]*)?\r?$", text)
    if section is None:
        raise host.HostError("Expected one explicit [network] section")
    end = re.search(r"(?m)^\[", text[section.end():])
    stop = section.end() + end.start() if end else len(text)
    body = text[section.end():stop]
    replacement, count = re.subn(r"(?m)^(upstream_port[ \t]*=[ \t]*)27891([ \t]*(?:#[^\r\n]*)?\r?)$",
                                 r"\g<1>17891\g<2>", body)
    if count != 1:
        raise host.HostError("Expected one plain upstream_port = 27891 setting")
    candidate = text[:section.end()] + replacement + text[stop:]
    expected = copy.deepcopy(original)
    expected["network"]["upstream_port"] = NEW_PORT
    if tomllib.loads(candidate) != expected:
        raise host.HostError("Proxy migration would modify another configuration field")
    return candidate.encode("utf-8")


def protected_file(path: Path) -> bytes:
    host.reject_symlinks(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
            or info.st_nlink != 1 or info.st_size > 1024 * 1024):
        raise host.HostError(f"Expected a protected root-owned regular file: {path}")
    return path.read_bytes()


def require_quiescent(config: dict) -> None:
    # systemctl returns 3 for inactive or failed; a manager/permission error must
    # not be mistaken for a safely stopped controller.
    state = host.run(["systemctl", "show", "--property=ActiveState", "--value",
                      "nexpoly-runner-controller.service"]).stdout.strip()
    if state not in {"inactive", "failed"}:
        raise host.HostError("Drain and stop the controller before switching its upstream")
    active = host.run(["systemctl", "list-units", "--no-legend", "--state=active,activating,deactivating",
                       "nexpoly-runner-vm@*.service"]).stdout.strip()
    if active:
        raise host.HostError("A VM is still active; retire it through runnerctl before switching upstream")
    controller = Path(config["paths"]["state"]) / "controller"
    try:
        drain = json.loads(protected_file(controller / "drain.json"))
        journal = json.loads(protected_file(controller / "state.json"))
    except (OSError, ValueError) as error:
        raise host.HostError("A valid drain marker and controller journal are required") from error
    if not isinstance(drain, dict) or not drain.get("reason"):
        raise host.HostError("The controller must remain drained during proxy migration")
    if not isinstance(journal, dict) or "instance" not in journal or journal["instance"] is not None:
        raise host.HostError("Registered standby/active VM remains in state; retire it before switching upstream")
    instances = Path(config["paths"]["state"]) / "instances"
    host.reject_symlinks(instances)
    if not instances.is_dir() or any(instances.iterdir()):
        raise host.HostError("Instance storage must be empty before switching upstream")


def proxy_health() -> None:
    # Explicit proxy; no inherited NO_PROXY may bypass the dedicated path.
    request = urllib.request.Request("https://github.com/robots.txt",
                                     headers={"User-Agent": "nexpoly-runner-upstream-migration/1"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": "http://127.0.0.1:27892"}))
    previous = {name: os.environ.pop(name) for name in ("no_proxy", "NO_PROXY") if name in os.environ}
    try:
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise host.HostError("New dedicated proxy path did not return HTTP 200")
            response.read(1024)
    except (OSError, ValueError) as error:
        raise host.HostError(f"New dedicated proxy path failed: {type(error).__name__}; controller remains stopped") from error
    finally:
        os.environ.update(previous)


def switch_upstream(source: Path) -> None:
    host.root_only()
    with host.installation_lock():
        original = protected_file(host.CONFIG)
        candidate = migrated_config(original)
        # Validate all fixed paths, budgets and ports before writing config.
        with tempfile.TemporaryDirectory(prefix=".nexpoly-upstream-", dir=host.CONFIG.parent) as temporary:
            temporary = Path(temporary)
            config_path = temporary / "config.toml"
            host.atomic_write(config_path, candidate, 0o600)
            config = host.read_config(config_path)
            require_quiescent(config)
            snapshot = temporary / "package"
            host.snapshot_package(source.absolute(), snapshot)
            backup = host.CONFIG.with_name(BACKUP_NAME)
            if candidate != original:
                if backup.exists():
                    if protected_file(backup) != original:
                        raise host.HostError("Existing migration backup differs; inspect it before proceeding")
                else:
                    host.atomic_write(backup, original, 0o600)
                host.atomic_write(host.CONFIG, candidate, stat.S_IMODE(host.CONFIG.stat().st_mode))
            # The normal installer preserves this now-migrated /etc config and
            # renders both Squid and nft from it. It cannot start the controller.
            host._install(snapshot, host.CONFIG)
        require_quiescent(config)
        if not host.firewall_check(config):
            raise host.HostError("Dedicated firewall does not match the new upstream; controller remains stopped")
        proxy_health()
    print("Runner upstream is 127.0.0.1:17891; dedicated Squid and UID firewall verified. "
          "Controller remains drained and stopped. No template rebuild or resume was attempted.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=PACKAGE)
    args = parser.parse_args()
    try:
        switch_upstream(args.source)
    except (host.HostError, OSError, ValueError) as error:
        reason = str(error) if isinstance(error, host.HostError) else type(error).__name__
        print(f"Upstream switch failed: {reason}. Keep the controller drained/stopped. "
              f"Any original config backup is {host.CONFIG.with_name(BACKUP_NAME)}; "
              "do not resume until config, installed package, proxy and firewall agree.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run storage checks through the private loopback-only M5 VM connection."""

import hashlib
import os
from pathlib import Path
import shlex
import stat
import subprocess

from openscience_m5_native_storage import ssh


class StorageGuest:
    def __init__(self, ssh_config):
        config = Path(ssh_config).absolute()
        if (
            config.name != "ssh.conf"
            or config.is_symlink()
            or not config.is_file()
            or config.stat().st_uid != os.getuid()
            or stat.S_IMODE(config.stat().st_mode) != 0o600
        ):
            raise ValueError("Expected a private M5 guest SSH configuration")
        # Every attestation still executes in the guest. Reuse only the encrypted
        # connection so concurrent web requests need not repeat the SSH handshake.
        directory = Path("/tmp") / f"nexpoly-m5-ssh-{os.getuid()}"
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("Expected a private SSH control socket directory")
        cfg_key = hashlib.sha256(
            os.fsencode(config.resolve()) + b"\0" + config.read_bytes()
        ).hexdigest()[:32]
        self.ssh = [
            *ssh(config.resolve().parent),
            "-T",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=60",
            "-o",
            f"ControlPath={directory / cfg_key}",
        ]

    def run(self, *argv, **kwargs):
        return subprocess.run(
            [*self.ssh, "m5-quota-guest", shlex.join(["sudo", "-n", *map(str, argv)])],
            check=True,
            **kwargs,
        )

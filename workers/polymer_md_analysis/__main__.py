"""Bind a separate private analysis socket; never opens the simulation socket."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import stat
import signal

import uvicorn

from .config import AnalysisSettings
from .main import create_app
from .files import open_root


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--uds", default=os.environ.get("POLYMER_MD_ANALYSIS_UDS"), required=not os.environ.get("POLYMER_MD_ANALYSIS_UDS"))
    args = parser.parse_args()
    path = Path(args.uds)
    if not path.is_absolute() or ".." in path.parts or path.name != "analysis.sock":
        parser.error("analysis requires its own absolute analysis.sock path")
    parent = open_root(path.parent)
    try:
        info = os.fstat(parent)
    finally:
        os.close(parent)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        parser.error("analysis socket directory must be private and owned")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            parser.error("analysis socket path is not an owned socket")
        with socket.socket(socket.AF_UNIX) as probe:
            probe.settimeout(.2)
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                current = path.lstat()
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    parser.error("analysis socket identity changed")
                path.unlink()
            else:
                parser.error("analysis service is already running")
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    path.chmod(0o600)
    identity = path.lstat()
    listener.listen(16)
    # Uvicorn replays SIGTERM after graceful shutdown. Turn that replay into
    # SystemExit so our inode-fenced socket cleanup still runs.
    previous_term = signal.getsignal(signal.SIGTERM)
    def completed_termination(_signal, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, completed_termination)
    try:
        uvicorn.run(create_app(AnalysisSettings.from_env()), fd=listener.fileno(), workers=1, access_log=False,
                    timeout_graceful_shutdown=3)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        listener.close()
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()

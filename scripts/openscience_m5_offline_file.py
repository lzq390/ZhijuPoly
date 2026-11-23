"""Operator recovery of a dev-m1 project file while its guarded workspace is cold."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

import openscience_per_user_dev as manager
from openscience_m5_backup import BACKUP_FREE_FLOOR, available_bytes, destination_path, digest, private_dir, publish
from openscience_m5_quota_bundle import disk_admission


def recover(ws, path, confirmation, target, max_output_bytes=None):
    registry = ws.registry
    if registry.data["format"] != 5 or not ws.nfs or ws.env != "dev-m1" or ws.user not in manager.PILOT_USERS:
        raise ValueError("Expected a guarded dev-m1 native workspace")
    if ws.resource("backend") or ws.resource("fixture"):
        raise ValueError("Recycle the selected workspace before offline file recovery")
    policy = ws.quota_status()
    aliases = manager.docker("volume", "ls", "-q", "--filter",
                             "label=org.nexpoly.m5.filesystem-uuid=" + policy["filesystem_uuid"]).splitlines()
    if ws.volume not in aliases or any(manager.docker("ps", "-aq", "--filter", "volume=" + alias) for alias in aliases):
        raise ValueError("Offline recovery requires a registered workspace volume with no attached containers")
    if confirmation and registry.disk_watermark()["state"] != "low":
        raise ValueError("Offline deletion is available only during verified low disk space")
    owner = {"environment": ws.env, "user_id": ws.user, "uid": ws.row["uid"], "workspace": ws.row["id"],
             "deployment": ws.deployment, "filesystem_uuid": policy["filesystem_uuid"]}
    nfs = manager.pilot_module("quota_nfs")
    client = Path(nfs.__file__).with_name("offline_file.py").read_text()
    # Run the normal live-mount attestation in the same unprivileged process that
    # reads the file. Keep request data in argv, outside the helper program.
    program = ("import sys\nrequest = sys.argv.pop()\n" + nfs.CLIENT_CHECK + "\n" + client
               + "\nowner, path, token, budget = json.loads(request)\n"
                 "receipt = project_file(root, path, owner, token, None if token else sys.stdout.buffer, budget)\n"
                 "print(json.dumps(receipt), file=sys.stderr)\n")
    command = ["docker", "run", "--rm", "--pull=never", "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--network=none", "--log-driver=none",
               "--memory=96m", "--cpus=0.5", "--pids-limit=32", "--user", ws.identity,
               *ws.label_flags("volume-check"), "--mount", ws.data_mount("/data") + ("" if confirmation else ",readonly"),
               manager.PYTHON, "python", "-ISB", "-c", program, *nfs.client_args(ws.nfs, ws.quota_binding, "/data"),
               json.dumps([owner, path, confirmation, max_output_bytes])]
    result = subprocess.run(command, stdout=target, stderr=subprocess.PIPE)
    if result.returncode:
        raise ValueError("Offline file helper failed: " + result.stderr.decode(errors="replace").strip()[-500:])
    receipt = json.loads(result.stderr.splitlines()[-1])
    if (any(receipt.get(k) != v for k, v in owner.items()) or receipt.get("path") != path
            or receipt.get("deleted") is not bool(confirmation)
            or (confirmation and receipt.get("confirmation") != confirmation)):
        raise ValueError("Offline file receipt differs from the requested operation")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--user-id", choices=sorted(manager.PILOT_USERS), required=True)
    parser.add_argument("--path", required=True, help="File relative to files/projects, including the project name")
    sub = parser.add_subparsers(dest="action", required=True)
    read = sub.add_parser("read")
    read.add_argument("--output", type=Path, required=True, help="New file in a private (0700) operator directory")
    read.add_argument("--reserve-bytes", type=int, default=BACKUP_FREE_FLOOR,
                      help="Minimum free bytes to retain on the operator copy disk")
    sub.add_parser("delete").add_argument("--confirm", required=True, help="Owner-bound confirmation from read")
    args = parser.parse_args()
    os.umask(0o077)
    manager.local_engine()
    # Hold the lifecycle reservation throughout the operation. Other running
    # users can still submit read-only disk admission checks for their writes.
    with manager.locked_registry(args.state_dir, False, operation="offline-file") as registry:
        ws = registry.workspace("dev-m1", args.user_id)
        if args.action == "delete":
            if not re.fullmatch("[a-f0-9]{64}", args.confirm):
                raise ValueError("Expected the complete read confirmation")
            receipt = recover(ws, args.path, args.confirm, subprocess.DEVNULL)
        else:
            if args.reserve_bytes <= 0:
                raise ValueError("A positive operator output disk reserve is required")
            output = destination_path(args.output)
            private_dir(output.parent)
            with disk_admission(output.parent), TemporaryDirectory(dir=output.parent) as stage:
                copy = Path(stage) / "file"
                with copy.open("x+b") as target:
                    fs_block = os.statvfs(output.parent).f_frsize
                    budget = available_bytes(output.parent) - args.reserve_bytes - fs_block
                    receipt = recover(ws, args.path, None, target, budget)
                    target.flush()
                    os.fsync(target.fileno())
                    target.seek(0)
                    if digest(target) != receipt["sha256"] or copy.stat().st_size != receipt["size"]:
                        raise ValueError("Offline copy differs from the source read receipt")
                publish(copy, output)
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()

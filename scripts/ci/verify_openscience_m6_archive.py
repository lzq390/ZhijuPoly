"""Exercise read-only, operator-side recovery of synthetic unassigned history on the M5 test VM."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile

from verify_openscience_m5_workspace_web import ROOT, json_write
from openscience_m5_native_storage import read
from openscience_m5_storage_ssh import StorageGuest


GUEST = """
import errno, hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path

blob = sys.stdin.buffer.read(1024 * 1024 + 1)
if len(blob) > 1024 * 1024 or hashlib.sha256(blob).hexdigest() != sys.argv[1]:
    raise ValueError("Unexpected fixture image")
pins = json.loads(Path("/etc/nexpoly-m5-disks.json").read_bytes())
expected = json.loads(sys.argv[3])
if any(pins.get("m5-" + owner) != identity for owner, identity in expected.items()):
    raise ValueError("Wrong isolated storage VM")
with tempfile.TemporaryDirectory(dir="/run", prefix="m6-offline-") as tmp:
    root = Path(tmp)
    archive, view, restore = root / "history.squashfs", root / "operator-mount", root / "restored"
    archive.write_bytes(blob)
    archive.chmod(0o400)
    view.mkdir(mode=0o700)
    subprocess.run(
        ["mount", "-t", "squashfs", "-o", "ro,loop,nodev,nosuid,noexec", str(archive), str(view)], check=True
    )
    try:
        mount = subprocess.check_output(
            ["findmnt", "-n", "-o", "FSTYPE,OPTIONS", "--target", str(view)], text=True
        ).split()
        if mount[0] != "squashfs" or "ro" not in mount[1].split(",") or not os.statvfs(view).f_flag & os.ST_RDONLY:
            raise ValueError("Archive is not a read-only filesystem")
        for path, mode in [(view / "unassigned.txt", "r+b"), (view / "new.txt", "xb")]:
            try:
                with path.open(mode) as sink:
                    sink.write(b"test")
            except OSError as error:
                if error.errno != errno.EROFS:
                    raise
            else:
                raise RuntimeError("Archive accepted a write")
        with restore.open("xb") as sink:
            sink.write((view / "unassigned.txt").read_bytes())
        digest = hashlib.sha256(restore.read_bytes()).hexdigest()
        if digest != sys.argv[2]:
            raise ValueError("Restored content differs")
    finally:
        subprocess.run(["umount", str(view)], check=True)
    if os.path.ismount(view):
        raise ValueError("Archive is still mounted")
    print(
        json.dumps(
            {
                "filesystem": "squashfs",
                "existing_file_write": "EROFS",
                "new_file_write": "EROFS",
                "restored_sha256": digest,
                "unmounted": True,
            }
        )
    )
"""


def run(vm, work):
    manifest = read(vm)
    baseline = json.loads((ROOT / "docs/verification/openscience-m5-workspace-quota-20261123.json").read_bytes())
    pins = baseline["native_registrations"]["src"]["native_claim"]["filesystem_uuids"]
    if any(manifest["workspaces"]["src-" + role]["filesystem_uuid"] != value for role, value in pins.items()):
        raise ValueError("Expected the retained dev-m1 quota VM")
    work.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(dir=work) as tmp:
        source = Path(tmp)
        content = ("M6 synthetic history without an assigned owner " + secrets.token_hex(16)).encode()
        (source / "unassigned.txt").write_bytes(content)
        archive = work / "synthetic-history.squashfs"
        subprocess.run(
            ["mksquashfs", source, archive, "-noappend", "-processors", "1", "-no-progress", "-quiet"], check=True
        )
    archive.chmod(0o400)
    blob = archive.read_bytes()
    image_sha, content_sha = (hashlib.sha256(data).hexdigest() for data in (blob, content))
    peer = StorageGuest(vm / "ssh.conf")
    result = peer.run(
        "python3",
        "-IB",
        "-c",
        GUEST,
        image_sha,
        content_sha,
        json.dumps(pins),
        input=blob,
        capture_output=True,
        timeout=60,
    )
    receipt = json.loads(result.stdout)
    if receipt["restored_sha256"] != content_sha or hashlib.sha256(archive.read_bytes()).hexdigest() != image_sha:
        raise ValueError("Fixture evidence differs")
    receipt.update(status="passed", scope="synthetic unassigned history", image_sha256=image_sha)
    json_write(work / "archive.json", receipt)
    print(json.dumps(receipt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    run(args.vm.resolve(strict=True), args.work.absolute())

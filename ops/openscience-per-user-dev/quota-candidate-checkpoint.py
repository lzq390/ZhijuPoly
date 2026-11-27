"""Read a quiesced candidate workspace, archive it on the registered backup disk, and verify a fresh restore."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory

sys.path.insert(0, "/inputs")
import quota_guest as guest


def restore_on_scratch(owner, standby, package, pin, expected):
    # The image lives on the standby's physically separate host disk. No registered workspace is moved.
    with NamedTemporaryFile(dir=standby, prefix=".candidate-restore-") as scratch, TemporaryDirectory(dir="/mnt") as mount:
        mount = Path(mount)
        scratch.truncate(3 * 1024**3)
        scratch.flush()
        label = guest.quota.volume_label(owner, "dst")
        guest.command("mkfs.ext4", "-q", "-F", "-O", "quota", "-b", "4096", "-m", "0", "-L", label, scratch.name)
        guest.command("mount", "-o", "loop,usrquota,nodev,nosuid,noexec", scratch.name, str(mount))
        try:
            result = guest.bundle.restore(mount, owner, package, pin)
            # Collect storage metadata, never user file contents.
            actual = result["quota"]
            guest.check(result["inventory"] == expected and actual["hard_bytes"] == guest.LIMIT
                        and actual["accounting"] and actual["enforced"] and actual["uid"] == owner["uid"],
                        "Restored inventory, quota or ownership differs")
            workspace = mount / "workspace"
            projects = workspace / "files/projects"
            return dict(inventory=result["inventory"], uid=actual["uid"], hard_bytes=actual["hard_bytes"],
                        filesystem_uuid=actual["filesystem_uuid"], projects=sum(p.is_dir() for p in projects.iterdir()),
                        project_files=sum(p.is_file() for p in projects.rglob("*")),
                        record_files=sum(p.is_file() for p in (workspace / "records").rglob("*")))
        finally:
            guest.command("umount", str(mount))


def main():
    os.umask(0o077)
    generation = sys.argv[1]
    if not re.fullmatch("[a-f0-9]{32}", generation):
        raise ValueError("Expected a fresh checkpoint ID")
    base = Path("/backup")
    users = json.loads(Path("/inputs/spec.json").read_bytes())["users"]
    disks = json.loads(Path("/etc/nexpoly-m5-disks.json").read_bytes())
    legs = {"src": "m5-", "dst": "m5-restore-"}
    guest.check(base.is_mount() and subprocess.check_output(["findmnt", "-n", "-T", str(base), "-o", "UUID"], text=True).strip() == disks["m5-backup"],
                "Backup device differs from its configured UUID")
    for phase, prefix in legs.items():
        for role, owner in users.items():
            mount = guest.MOUNT_ROOT / (phase + "-" + role)
            observed = guest.quota.read(mount, owner)
            guest.check(owner["environment"] == "deploy-m6" and observed["filesystem_uuid"] == disks[prefix + role]
                        and observed["hard_bytes"] == guest.LIMIT, "Wrong destination or source workspace")
    directory = base / ("checkpoint-" + generation)
    results = {}
    with TemporaryDirectory(dir=base, prefix=".checkpoint-") as temp:
        stage = Path(temp) / "checkpoint"
        stage.mkdir(mode=0o700)
        for role, owner in users.items():
            source = guest.MOUNT_ROOT / ("src-" + role)
            standby = guest.MOUNT_ROOT / ("dst-" + role)
            package = stage / role
            saved = guest.bundle.backup(source, owner, package, reserve_bytes=512 * guest.MIB)
            restored = restore_on_scratch(owner, standby, package, saved["manifest_sha256"], saved["inventory"])
            guest.check(restored["filesystem_uuid"] not in disks.values(), "Temporary target UUID must be new")
            results[role] = {"manifest_sha256": saved["manifest_sha256"], "source_inventory": saved["inventory"],
                             "restored": restored}
        guest.bundle.publish(stage, directory)
    print(json.dumps({"status": "passed", "generation": generation, "accounts": results}))


if __name__ == "__main__":
    main()

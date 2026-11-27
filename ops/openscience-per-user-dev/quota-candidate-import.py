"""Restore a trusted A/B checkpoint into a new, offline deploy-m6 target guest."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory

sys.path.insert(0, "/inputs")
import quota_guest as guest  # noqa: E402


def receive(directory, pins):
    expected = {f"{role}/{name}" for role in "ab" for name in ("manifest.json", "workspace.tar")}
    # Copy only the four ordinary files. Tar metadata never controls guest paths or permissions.
    with TemporaryDirectory(dir=guest.BACKUP, prefix=".incoming-") as tmp:
        received = {}
        repeat = directory.exists()
        package = directory if repeat else Path(tmp) / "package"
        if repeat:
            guest.bundle.private_dir(package)
            for role in "ab":
                guest.bundle.private_dir(package / role)
        else:
            package.mkdir(mode=0o700)
            for role in "ab":
                (package / role).mkdir(mode=0o700)
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as stream:
            for item in stream:
                if item.name not in expected or not item.isreg() or item.sparse is not None or item.size < 0:
                    raise ValueError("Unexpected member in the incoming checkpoint")
                expected.remove(item.name)
                if item.name.endswith("/manifest.json") and item.size > 65536:
                    raise ValueError("Oversized quota manifest")
                with stream.extractfile(item) as src:
                    if repeat:
                        # A full pair can occupy most of the exchange disk. Verify a retry in flight.
                        received[item.name] = guest.bundle.digest(src)
                        continue
                    guest.bundle.require_space(guest.bundle.available_bytes(package), item.size, 512 * guest.MIB)
                    with (package / item.name).open("xb") as out:
                        shutil.copyfileobj(src, out)
                        out.flush()
                        os.fsync(out.fileno())
        guest.check(not expected, "Incomplete checkpoint transfer")
        for role, pin in zip("ab", pins):
            with guest.bundle.private_file(package / role / "manifest.json") as saved:
                raw = saved.read(65537)
            guest.check(len(raw) <= 65536 and hashlib.sha256(raw).hexdigest() == pin, "Manifest hash differs")
            archive_hash = json.loads(raw)["archive_sha256"]
            with guest.bundle.private_file(package / role / "workspace.tar") as saved:
                guest.check(guest.bundle.digest(saved) == archive_hash, "Archive hash differs")
            if repeat:
                guest.check(received[role + "/manifest.json"] == pin and received[role + "/workspace.tar"] == archive_hash,
                            "Repeated transfer differs from its published package")
        if not repeat:
            guest.bundle.publish(package, directory)


def import_checkpoint(generation, pins):
    if not re.fullmatch(r"[a-f0-9]{32}", generation) or any(not re.fullmatch(r"[a-f0-9]{64}", pin) for pin in pins):
        raise ValueError("Expected the checkpoint and the two independently recorded manifest hashes")
    spec = json.loads(Path("/inputs/spec.json").read_bytes())
    users = spec["users"]
    guest.check(spec.get("restore_target") is True and spec["environment"] == "deploy-m6" and set(users) == {"a", "b"},
                "Expected an independently provisioned restore target")
    status = subprocess.check_output(["systemctl", "show", "m5-native-nfs.service", "-p", "ActiveState", "--value"], text=True).strip()
    guest.check(status == "inactive", "Restore requires offline exports")
    root = guest.BACKUP / ("checkpoint-" + generation)
    disks = json.loads(Path("/etc/nexpoly-m5-disks.json").read_bytes())
    guest.check(guest.BACKUP.is_mount() and
                subprocess.check_output(["findmnt", "-n", "-T", str(guest.BACKUP), "-o", "UUID"], text=True).strip() == disks["m5-backup"],
                "Incoming packages must be on the guest backup disk")
    receive(root, pins)
    guest.bundle.private_dir(root)
    targets = {}
    for role, pin in zip("ab", pins):
        owner = users[role]
        package = root / role
        mount = guest.MOUNT_ROOT / ("dst-" + role)
        guest.bundle.private_dir(package)
        with guest.bundle.private_file(package / "manifest.json") as mf:
            raw = mf.read(65537)
            guest.check(hashlib.sha256(raw).hexdigest() == pin and len(raw) <= 65536, "Manifest digest differs")
            meta = json.loads(raw)
            quota = guest.quota.read(mount, owner)
            guest.check(meta["quota"]["identity"] == {key: owner[key] for key in ("environment", "user_id", "workspace", "uid")}
                        and quota["filesystem_uuid"] == disks["m5-restore-" + role]
                        and quota["filesystem_uuid"] != meta["quota"]["source_filesystem_uuid"]
                        and meta["quota"]["hard_bytes"] == guest.LIMIT, "Unexpected restore identity, quota or filesystem")
        with guest.bundle.private_file(package / "workspace.tar") as fh:
            guest.check(guest.bundle.digest(fh) == meta["archive_sha256"], "Received archive digest differs")
            fh.seek(0)
            reservations = guest.bundle.archived_reservations(fh)
        targets[role] = (mount, owner, pin, package, meta, reservations)
    result = {}
    for role, (mount, owner, pin, package, meta, reservations) in targets.items():
        workspace = mount / "workspace"
        if not workspace.exists():
            guest.bundle.restore(mount, owner, package, pin)
        # If the other participant failed, retry the same pair without overwriting a published workspace.
        inventory = guest.bundle.inventory(workspace, owner, reservations=reservations)
        effective = guest.quota.read(mount, owner)
        st = workspace.stat()
        guest.check(inventory == meta["inventory"] and effective["hard_bytes"] == guest.LIMIT and
                    effective["enforced"] and effective["accounting"] and
                    st.st_uid == owner["uid"] == effective["uid"] and st.st_gid == owner["gid"],
                    "Restored inventory, enforcement or ownership mismatch")
        other = users["a" if role == "b" else "b"]
        foreign = guest.run_as(other, guest.private_read_denied, workspace / ".m1-owner")
        result[role] = dict(source_uuid=meta["quota"]["source_filesystem_uuid"],
                            destination_uuid=effective["filesystem_uuid"], uid=st.st_uid, gid=st.st_gid,
                            hard_bytes=effective["hard_bytes"], accounting=effective["accounting"],
                            enforced=effective["enforced"], inventory=inventory, archive_sha256=meta["archive_sha256"],
                            manifest_sha256=pin, foreign_read_errno=foreign)
    return {"status": "passed", "checkpoint": generation, "accounts": result}


if __name__ == "__main__":
    os.umask(0o077)
    checkpoint, a, b = sys.argv[1:]
    print(json.dumps(import_checkpoint(checkpoint, (a, b)), sort_keys=True))

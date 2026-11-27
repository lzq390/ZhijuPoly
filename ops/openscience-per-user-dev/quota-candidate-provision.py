"""Initialize four blank candidate filesystems and verify the two registered users before NFS starts."""

import errno
import json
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, "/inputs")
import quota_guest as guest


NOTE = Path("files/general/storage-check.txt")
PROBE = Path("files/general/storage-write-check.bin")


def seed(workspace, message):
    for name in sorted(guest.archive.SKELETON - {"."}, key=lambda s: (s.count("/"), s)):
        (workspace / name).mkdir(mode=0o700)
    (workspace / NOTE).write_bytes(message)


def full_pair(volumes, users, phase):
    a, b = users["a"], users["b"]
    mount = volumes[phase, "a"]
    wa, wb = mount / "workspace", volumes[phase, "b"] / "workspace"
    before = guest.quota.read(mount, a)
    note = guest.run_as(a, guest.file_facts, wa / NOTE)
    guest.run_as(a, guest.reserve_to_limit, wa, before["hard_bytes"], before["used_bytes"])
    try:
        denied = guest.run_as(a, guest.normal_write, wa / PROBE, bytes(4096), True)
        guest.check(denied["errno"] == errno.EDQUOT, "Expected kernel hard quota")
        guest.check(guest.run_as(a, guest.file_facts, wa / NOTE) == note, "Full account lost its original note")
        b_write = guest.run_as(b, guest.normal_write, wb / PROBE, secrets.token_bytes(4096), False)
        guest.run_as(b, Path.unlink, wb / PROBE)
    finally:
        guest.run_as(a, guest.release, wa)
    retry = guest.run_as(a, guest.normal_write, wa / PROBE, secrets.token_bytes(4096), False)
    guest.run_as(a, Path.unlink, wa / PROBE)
    return {"a_new_write_errno": denied["errno"], "a_read_sha256": note["sha256"],
            "b_concurrent_write_sha256": b_write["sha256"], "a_retry_sha256": retry["sha256"]}


def main():
    spec = json.loads((guest.INPUTS / "spec.json").read_bytes())
    users = spec["users"]
    guest.check(spec["environment"] == "deploy-m6" and set(users) == {"a", "b"}, "Unexpected candidate")
    volumes = guest.mount_volumes(guest.blank_volumes(), [], users)
    for role, owner in users.items():
        mount = volumes["src", role]
        guest.quota.install(mount, owner)
        ws = mount / "workspace"
        ws.mkdir(mode=0o700)
        os.chown(ws, owner["uid"], owner["gid"])
        marker = ws / ".m1-owner"
        marker.write_text(owner["workspace"])
        marker.chmod(0o444)
        guest.run_as(owner, seed, ws, secrets.token_bytes(64))
    if spec.get("restore_target") is True:
        for role, owner in users.items():
            target = volumes["dst", role]
            guest.check(not (target / "workspace").exists() and guest.quota.read(target, owner)["hard_bytes"] == 0,
                        "Restore target must start empty")
        return
    test = {"src": full_pair(volumes, users, "src")}
    snapshots = {r: guest.bundle.backup(volumes["src", r], users[r], guest.BACKUP / r,
                                          reserve_bytes=512 * guest.MIB) for r in users}
    result = {}
    for role, owner in users.items():
        other = "b" if role == "a" else "a"
        target = volumes["dst", role]
        try:
            guest.bundle.restore(target, owner, guest.BACKUP / other, snapshots[other]["manifest_sha256"])
        except ValueError as error:
            guest.check("does not belong to the target binding" in str(error), str(error))
        else:
            raise ValueError("Accepted the wrong user's backup")
        guest.check(not (target / "workspace").exists() and guest.quota.read(target, owner)["hard_bytes"] == 0,
                    "Wrong-owner restore changed destination")
        restored = guest.bundle.restore(target, owner, guest.BACKUP / role, snapshots[role]["manifest_sha256"])
        before = guest.quota.read(volumes["src", role], owner)
        after = guest.quota.read(target, owner)
        guest.check(before["uid"] == after["uid"] == owner["uid"] and before["enforced"] and after["enforced"]
                    and before["accounting"] and after["accounting"] and before["filesystem_uuid"] != after["filesystem_uuid"]
                    and before["hard_bytes"] == after["hard_bytes"] == guest.LIMIT
                    and snapshots[role]["inventory"] == restored["inventory"], "Recovery differs")
        foreign = guest.run_as(users[other], guest.private_read_denied, target / "workspace" / NOTE)
        result[role] = {"source": before, "restored": after, "owner": owner, "foreign_read_errno": foreign,
                        "wrong_owner_restore_refused": True, "manifest_sha256": snapshots[role]["manifest_sha256"],
                        "inventory": restored["inventory"]}
    test["dst"] = full_pair(volumes, users, "dst")
    output = {"format": "nexpoly-current-host-storage-v1", "status": "passed", "accounts": result, "full": test}
    path = Path("/var/lib/nexpoly-candidate-storage.json")
    with path.open("x") as out:
        json.dump(output, out, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())


if __name__ == "__main__":
    main()

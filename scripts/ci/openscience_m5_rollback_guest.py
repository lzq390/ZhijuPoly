"""Validate the two registered quota filesystems and maintain only this rollback drill's fixtures."""

import json
import os
from pathlib import Path
import re
import runpy
import stat
import sys


def main(action, token, expected):
    life = runpy.run_path("/opt/nexpoly/scripts/ci/openscience_m5_guest_lifecycle.py")
    guest, quota, require = life["guest"], life["quota"], life["require"]
    require(action in ("seed", "fill", "cleanup") and re.fullmatch("[0-9a-f]{32}", token), "Invalid test action or ID")
    users = guest["input_bindings"]()
    require(set(users) == set(expected) == {"a", "b"}, "Expected both dev-m1 owners")
    prefix = "m5-rb-" + token

    def paths(role):
        disk = life["mount"]("src", role)
        workspace = disk / "workspace"
        project = workspace / guest["NOTEBOOK"].parent
        general = workspace / "files/general"
        require(all(p.resolve(strict=True) == p for p in (workspace, general, project)), "Unexpected test path")
        return (workspace, project / (prefix + "-edit.txt"), project / (prefix + "-release.txt"),
                general / ("." + prefix))

    def facts(role):
        user = users[role]
        workspace, *_ = paths(role)
        kernel = quota.read(workspace.parent, user)
        note = life["notebook"]("src", role, user)
        require(user["environment"] == "dev-m1" and user["user_id"] == "test-user-" + role
                and kernel["volume_label"] == "m5quota-src-" + role and kernel["filesystem_type"] == "ext4"
                and kernel["accounting"] and kernel["enforced"] and kernel["hard_bytes"] == guest["LIMIT"]
                and kernel["filesystem_uuid"] == expected[role]["uuid"]
                and kernel["uid"] == user["uid"] == user["gid"] == workspace.stat().st_uid == note["uid"]
                and (workspace / ".m1-owner").read_text() == user["workspace"] == expected[role]["workspace"]
                and note == expected[role]["notebook"], "Test disk, owner binding or baseline notebook differs")
        return {"kernel": kernel, "notebook": note}

    def seed(role):
        _, editor, release, scratch = paths(role)
        scratch.mkdir(mode=0o700)
        if role == "a":
            (scratch / "files/general").mkdir(parents=True, mode=0o700)
            life["release_candidate"](release)
        with editor.open("x") as stream:
            stream.write(f"M5 rollback {token} {users[role]['user_id']}\n")
            stream.flush()
            os.fsync(stream.fileno())

    def cleanup(role):
        _, editor, release, scratch = paths(role)
        files = [editor]
        dirs = [scratch]
        if role == "a":
            files.extend((release, scratch / guest["FILLER"]))
            dirs = [scratch / "files/general", scratch / "files", scratch]
        for path in dirs:
            if os.path.lexists(path):
                info = path.lstat()
                require(path.resolve() == path and stat.S_ISDIR(info.st_mode) and info.st_uid == users[role]["uid"],
                        "Unexpected fixture directory or owner")
        for path in files:
            if os.path.lexists(path):
                info = path.lstat()
                require(path.resolve() == path and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                        and info.st_uid == users[role]["uid"], "Unexpected fixture file or owner")
                path.unlink()
        for path in dirs:
            if path.exists():
                path.rmdir()

    before = {role: facts(role) for role in users}
    if action == "seed":
        require(all(not os.path.lexists(p) for role in users for p in paths(role)[1:]), "Test name already exists")
        try:
            for role, user in users.items():
                guest["run_as"](user, seed, role)
        except BaseException:
            for role, user in users.items():
                guest["run_as"](user, cleanup, role)
            raise
    elif action == "fill":
        workspace, editor, release, scratch = paths("a")
        require(release.stat().st_blocks * 512 >= 2 * 1024**2 and editor.is_file()
                and scratch.resolve(strict=True) == scratch, "A's release or reservation fixture changed")
        allocated = guest["run_as"](users["a"], guest["reserve_to_limit"], scratch,
                                    guest["LIMIT"], before["a"]["kernel"]["used_bytes"])
        a = facts("a")
        fs = os.statvfs(workspace)
        require(0 <= guest["LIMIT"] - a["kernel"]["used_bytes"] < fs.f_frsize and fs.f_bavail > 0,
                "A did not reach only its own UID limit")
        return {"a": a, "b": facts("b"), "allocated_bytes": allocated,
                "filesystem_free_bytes": fs.f_bavail * fs.f_frsize}
    else:
        for role, user in users.items():
            guest["run_as"](user, cleanup, role)
            require(all(not os.path.lexists(p) for p in paths(role)[1:]), "Rollback fixture still exists")
    return {role: facts(role) for role in users}


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))))

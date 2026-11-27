"""Create and identify the single release file in the M6 cold cleanup drill."""

import hashlib
import json
import os
from pathlib import Path
import stat
import sys


def verify_file(file, uid, links, size, expected):
    info = os.fstat(file.fileno())
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != links or info.st_uid != uid
            or info.st_size != size or info.st_blocks * 512 < size):
        raise ValueError("Unexpected release file allocation or ownership")
    if hashlib.file_digest(file, "sha256").digest() != expected.digest():
        raise ValueError("Unexpected release file content")
    return info


def release_file(action, folder, tag, uid):
    name = f"release-{tag}.txt"
    path = folder / name
    if action == "create" and os.path.lexists(path):
        return {"name_in_use": True}
    block = (f"A {tag} release".encode().ljust(1023, b".") + b"\n") * 1024
    size = len(block) * 96
    expected = hashlib.sha256()
    for _ in range(96):
        expected.update(block)
    if action == "create":
        # An unlinked ext4 inode releases its blocks on close, even if the guest
        # writer dies halfway through. Publish without replacing an existing name.
        fd = os.open(folder, os.O_TMPFILE | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w+b") as file:
            for _ in range(96):
                file.write(block)
            file.flush()
            os.fsync(fd)
            file.seek(0)
            info = verify_file(file, uid, 0, size, expected)
            directory = os.open(folder, os.O_DIRECTORY | os.O_RDONLY)
            try:
                try:
                    os.link(f"/proc/self/fd/{fd}", name, dst_dir_fd=directory)
                except FileExistsError:
                    return {"name_in_use": True}
                os.fsync(directory)
            finally:
                os.close(directory)
    else:
        if action not in {"check", "cleanup"}:
            raise ValueError("Unknown release file action")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"absent": True}
        with os.fdopen(fd, "rb") as file:
            info = verify_file(file, uid, 1, size, expected)
        if action == "cleanup":
            if not os.path.samestat(info, path.lstat()):
                raise ValueError("Release file changed during verification")
            path.unlink()
    return {"size": info.st_size, "allocated_bytes": info.st_blocks * 512, "sha256": expected.hexdigest()}


def main():
    sys.path[:0] = ["/opt/nexpoly/scripts", "/opt/nexpoly/ops/openscience-per-user-dev", "/inputs"]
    import water_pool as pool
    from quota_guest import run_as

    action, tag, project, fs, uid = sys.argv[1:]
    cfg, roles = pool.topology()
    state = pool.observation(cfg, roles)["workspaces"]["a"]
    assert state["uid"] == int(uid) and state["filesystem_uuid"] == fs
    folder = pool.ROOTS / "src-a/workspace/files/projects" / project
    binding = json.loads(Path("/inputs/spec.json").read_bytes())["users"]["a"]
    assert binding["uid"] == int(uid)
    print(json.dumps(run_as(binding, release_file, action, folder, tag, int(uid))))


if __name__ == "__main__":
    main()

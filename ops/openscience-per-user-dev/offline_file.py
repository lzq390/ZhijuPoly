"""Read or release a cold workspace's project file, running as its unprivileged UID."""

from contextlib import ExitStack
import hashlib
import json
import os
import stat


def project_file(root, path, owner, confirmation, output, max_output_bytes=None):
    # Path is relative to files/projects and includes the project directory.
    parts = path.split("/")
    if len(parts) < 2 or any(not part or part.startswith(".") for part in parts):
        raise ValueError("Expected a non-hidden project and relative project file")
    uid = owner["uid"]
    if (os.getuid(), os.getgid()) != (uid, uid):
        raise ValueError("Offline file helper is not running as the workspace owner")
    flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY
    with ExitStack() as stack:
        parent = os.open(root, flags)
        stack.callback(os.close, parent)
        device = os.fstat(parent).st_dev
        for part in ["files", "projects", *parts[:-1]]:
            parent = os.open(part, flags, dir_fd=parent)
            stack.callback(os.close, parent)
            info = os.fstat(parent)
            if (info.st_dev, info.st_uid, info.st_gid) != (device, uid, uid):
                raise ValueError("Unexpected project directory owner or filesystem")
        name = parts[-1]
        fd = os.open(name, os.O_NOFOLLOW | os.O_RDONLY | os.O_NONBLOCK, dir_fd=parent)
        source = stack.enter_context(os.fdopen(fd, "rb"))
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or (before.st_dev, before.st_uid, before.st_gid) != (device, uid, uid)):
            raise ValueError("Expected a single-link regular file owned by this workspace")
        if output is not None and before.st_size > max_output_bytes:
            raise ValueError("Operator destination lacks space for the complete file and its reserve")
        digest = hashlib.sha256()
        while block := source.read(1024 * 1024):
            digest.update(block)
            if output is not None:
                output.write(block)
        # NFS can retain an open unlinked inode under a .nfs name. The host
        # holds the lifecycle lock and has verified the absence of clients.
        source.close()
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
        if any(getattr(current, key) != getattr(before, key) for key in fields):
            raise ValueError("The project file changed during offline recovery")
        receipt = {**owner, "path": path, "size": before.st_size, "sha256": digest.hexdigest(),
                   "inode": before.st_ino, "ctime_ns": before.st_ctime_ns}
        token = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
        if confirmation:
            if token != confirmation:
                raise ValueError("Read confirmation differs from this owner, path or file version")
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
        return {**receipt, "confirmation": token, "deleted": bool(confirmation)}

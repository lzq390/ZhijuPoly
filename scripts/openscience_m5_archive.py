"""Offline M1 workspace data format. Only this module runs inside the volume helper."""

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tarfile


ROOT_DIRS = {"cache", "config", "files", "home", "records", "state"}
SKELETON = {".", *ROOT_DIRS, "files/general", "files/projects"}
CHUNK = 1024 * 1024


def permitted(name):
    parts = name.split("/")
    if name == ".":
        return True
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if name == ".m1-owner" or name in SKELETON:
        return True
    return parts[0] == "records" or (parts[0] == "files" and parts[1] != ".runtime")


def validate_entry(name, folder, mode, owner, group, uid):
    if not permitted(name) or mode < 0 or mode > 0o777:
        raise ValueError("Unexpected workspace entry or special mode")
    if name == ".m1-owner":
        if (folder, mode, owner, group) != (False, 0o444, 0, 0):
            raise ValueError("Unexpected workspace identity marker metadata")
    elif (owner, group) != (uid, uid):
        raise ValueError("Unexpected workspace entry ownership")
    if name in SKELETON and not folder:
        raise ValueError("Expected a workspace directory")
    if name == "." and mode != 0o700:
        raise ValueError("Expected private workspace root")


def entries(root):
    def walk(path, name):
        yield path, name
        if name in ROOT_DIRS - {"files", "records"}:
            return
        if path.is_dir() and not path.is_symlink():
            for child in sorted(path.iterdir(), key=lambda p: p.name):
                relative = child.name if name == "." else name + "/" + child.name
                if relative == "files/.runtime":
                    continue
                if not permitted(relative):
                    raise ValueError("Unrecognized top-level volume entry; review backup scope")
                yield from walk(child, relative)

    return walk(root, ".")


def add_tree(root, archive, uid, workspace):
    for path, name in entries(root):
        info = path.lstat()
        folder = stat.S_ISDIR(info.st_mode)
        if not folder and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError("Only directories and single-link regular files are supported")
        validate_entry(name, folder, stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid, uid)
        member = tarfile.TarInfo(name)
        member.mode, member.uid, member.gid = stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid
        member.mtime = int(info.st_mtime)
        if folder:
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        else:
            member.size = info.st_size
            with path.open("rb") as source:
                if name == ".m1-owner":
                    if source.read(128) != workspace.encode():
                        raise ValueError("Workspace identity marker mismatch")
                    source.seek(0)
                archive.addfile(member, source)


def process_archive(stream, uid, workspace, target=None):
    """Validate before restore; during restore only create new regular files/directories."""
    tree_hash = hashlib.sha256()
    files = size = 0
    seen, directories, metadata = set(), set(), []
    if target is not None and any(target.iterdir()):
        raise ValueError("Restore requires an empty volume")
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            name = member.name
            folder = member.isdir()
            if (
                not (folder or member.type in {tarfile.REGTYPE, tarfile.AREGTYPE})
                or member.size < 0
                or (folder and member.size)
            ):
                raise ValueError("Unsupported archive entry")
            validate_entry(name, folder, member.mode, member.uid, member.gid, uid)
            if name in seen or (name != "." and str(Path(name).parent) not in directories):
                raise ValueError("Duplicate entry or invalid archive order")
            seen.add(name)
            dest = target / name if target is not None else None
            checksum = None
            if folder:
                directories.add(name)
                if dest is not None and name != ".":
                    dest.mkdir(mode=0o700)
            else:
                if name == ".m1-owner" and member.size != len(workspace):
                    raise ValueError("Invalid workspace marker size")
                contents = archive.extractfile(member)
                calculated = hashlib.sha256()
                written = 0
                sink = dest.open("xb") if dest is not None else None
                try:
                    while block := contents.read(CHUNK):
                        if name == ".m1-owner" and block != workspace.encode():
                            raise ValueError("Workspace identity marker mismatch")
                        written += len(block)
                        calculated.update(block)
                        if sink:
                            sink.write(block)
                finally:
                    if sink:
                        sink.close()
                if written != member.size:
                    raise ValueError("Incomplete workspace archive")
                files += 1
                size += member.size
                checksum = calculated.hexdigest()
            if dest is not None:
                metadata.append((dest, member))
            record = [name, folder, member.mode, member.uid, member.gid, member.size, checksum]
            tree_hash.update(json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode() + b"\n")
    if not (SKELETON | {".m1-owner"}) <= seen:
        raise ValueError("Incomplete workspace directory layout")
    for path, member in reversed(metadata):
        os.chown(path, member.uid, member.gid)
        path.chmod(member.mode)
        os.utime(path, (member.mtime, member.mtime))
        # Persist this restored object after its final metadata. Reverse traversal
        # commits each child and directory entry before its containing directory.
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if member.isdir():
            flags |= os.O_DIRECTORY
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return {
        "tree_sha256": tree_hash.hexdigest(),
        "files": files,
        "directories": len(directories),
        "file_bytes": size,
    }


if __name__ == "__main__":
    operation, uid_text, workspace_id = sys.argv[1:]
    volume = Path("/data")
    if operation == "pack":
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            add_tree(volume, tar, int(uid_text), workspace_id)
    elif operation == "unpack":
        print(json.dumps(process_archive(sys.stdin.buffer, int(uid_text), workspace_id, volume)))
    elif operation == "device":
        dev = volume.stat().st_dev
        print(f"{os.major(dev)}:{os.minor(dev)}")
    else:
        raise SystemExit("Unknown operation")

"""Access project files on a cold workspace as its unprivileged owner."""

import base64
from contextlib import contextmanager, ExitStack
import ctypes
import errno
import hashlib
import json
import os
import stat
import struct
import time

PAGE_SIZE = 32
SCAN_LIMIT = 128
READ_BYTES = 16 * 1024


def components(path, directory=False):
    if path == "" and directory:
        return []
    parts = path.split("/")
    if (len(parts) < (1 if directory else 2) or len(path.encode()) > 2048
            or any(not visible_name(part) for part in parts)):
        raise ValueError("Expected a non-hidden relative project path")
    return parts


def visible_name(name):
    return (bool(name) and not name.startswith(".") and "/" not in name
            and len(os.fsencode(name)) <= 255 and all(c.isprintable() for c in name))


@contextmanager
def project_directory(root, parts, uid):
    if (os.getuid(), os.getgid()) != (uid, uid):
        raise ValueError("Offline file helper is not running as the workspace owner")
    flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY
    with ExitStack() as stack:
        parent = os.open(root, flags)
        stack.callback(os.close, parent)
        device = os.fstat(parent).st_dev
        for part in ["files", "projects", *parts]:
            parent = os.open(part, flags, dir_fd=parent)
            stack.callback(os.close, parent)
            info = os.fstat(parent)
            if (info.st_dev, info.st_uid, info.st_gid) != (device, uid, uid):
                raise ValueError("Unexpected project directory owner or filesystem")
        yield parent, device


def regular(info, device, uid):
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and (info.st_dev, info.st_uid, info.st_gid) == (device, uid, uid))


def file_version(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def token_for(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def project_file(root, path, owner, confirmation, output, max_output_bytes=None):
    parts = path.split("/")
    if len(parts) < 2 or any(not part or part.startswith(".") for part in parts):
        raise ValueError("Expected a non-hidden project and relative project file")
    uid = owner["uid"]
    with project_directory(root, parts[:-1], uid) as (parent, device):
        name = parts[-1]
        fd = os.open(name, os.O_NOFOLLOW | os.O_RDONLY | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(fd)
            if not regular(before, device, uid):
                raise ValueError("Expected a single-link regular file owned by this workspace")
            if output is not None and before.st_size > max_output_bytes:
                raise ValueError("Operator destination lacks space for the complete file and its reserve")
            digest = hashlib.sha256()
            while block := source.read(1024 * 1024):
                digest.update(block)
                if output is not None:
                    output.write(block)
        # Close the NFS read handle before unlink so the allocation can be reclaimed.
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if file_version(current) != file_version(before):
            raise ValueError("The project file changed during offline recovery")
        receipt = {**owner, "path": path, "size": before.st_size, "sha256": digest.hexdigest(),
                   "inode": before.st_ino, "ctime_ns": before.st_ctime_ns}
        token = token_for(receipt)
        if confirmation:
            if token != confirmation:
                raise ValueError("Read confirmation differs from this owner, path or file version")
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
        return {**receipt, "confirmation": token, "deleted": bool(confirmation)}


def directory_entries(parent, cookie):
    # On our Linux ext4/NFSv4.1 mounts d_off is the opaque cookie for the
    # next record. scandir hides it and buffers more names than a UI page.
    os.lseek(parent, cookie, os.SEEK_SET)
    getdents = ctypes.CDLL(None, use_errno=True).getdents64
    getdents.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    getdents.restype = ctypes.c_ssize_t
    buffer = ctypes.create_string_buffer(8192)
    header = struct.Struct("=QqHB")  # linux_dirent64: ino, off, reclen, type
    while True:
        size = getdents(parent, buffer, len(buffer))
        if size < 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))
        if not size:
            return
        block = buffer.raw[:size]
        pos = 0
        while pos < size:
            _, offset, length, _ = header.unpack_from(block, pos)
            name = block[pos + header.size:pos + length].split(b"\0", 1)[0]
            yield os.fsdecode(name), offset
            pos += length


def list_directory(parent, device, owner, path, after, until_ms):
    before = os.fstat(parent)
    stamp = token_for([owner, path, before.st_ino, before.st_mtime_ns, before.st_ctime_ns])
    cookie, lower_bound = 0, after
    if after.startswith("/1/"):
        _, _, previous, encoded, lower_bound = after.split("/", 4)
        digits = encoded.removeprefix("-")
        if not digits.isascii() or not digits.isdecimal() or len(digits) > 19:
            raise ValueError("Invalid directory cookie")
        cookie = int(encoded)
        if not -(2**63) <= cookie < 2**63 or previous != stamp:
            raise FileExistsError("cleanup_changed")
    # Old pages used the last visible filename as a lexical lower bound.
    # Preserve that filter on every continuation while switching to cookies.
    if lower_bound and not visible_name(lower_bound):
        raise ValueError("Invalid directory cursor")
    uid = owner["uid"]
    entries = []
    next_cursor = None
    for scanned, (name, offset) in enumerate(directory_entries(parent, cookie), 1):
        check_deadline(until_ms)
        if name > lower_bound and visible_name(name):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                raise FileExistsError("cleanup_changed") from None
            if (info.st_dev, info.st_uid, info.st_gid) == (device, uid, uid):
                if stat.S_ISDIR(info.st_mode):
                    entries.append({"name": name, "kind": "directory", "size": None})
                elif path and regular(info, device, uid):
                    entries.append({"name": name, "kind": "file", "size": info.st_size})
        if len(entries) == PAGE_SIZE or scanned >= SCAN_LIMIT or time.time() * 1000 >= until_ms - 1000:
            next_cursor = f"/1/{stamp}/{offset}/{lower_bound}"
            break
    check_deadline(until_ms)
    now = os.fstat(parent)
    if (now.st_mtime_ns, now.st_ctime_ns) != (before.st_mtime_ns, before.st_ctime_ns):
        raise FileExistsError("cleanup_changed")
    return {"entries": entries, "next": next_cursor}


def check_deadline(until_ms):
    if time.time() * 1000 >= until_ms:
        raise TimeoutError("cleanup_expired")


def browse_file(root, owner, action, data, until_ms):
    check_deadline(until_ms)
    uid, path = owner["uid"], data["path"]
    listing = action == "list"
    parts = components(path, directory=listing)
    with project_directory(root, parts if listing else parts[:-1], uid) as (parent, device):
        if listing:
            try:
                return list_directory(parent, device, owner, path, data["after"], until_ms)
            except OSError as error:
                if data["after"].startswith("/1/") and error.errno in (errno.EINVAL, errno.ESTALE):
                    raise FileExistsError("cleanup_changed") from error
                raise
        name = parts[-1]
        fd = os.open(name, os.O_NOFOLLOW | os.O_RDONLY | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(fd)
            if not regular(before, device, uid):
                raise FileNotFoundError()
            # NFS may allocate a new client device number on the next mount.
            # The owner already includes the attested persistent filesystem UUID.
            version = token_for([owner, path, file_version(before)[1:]])
            if data.get("version", version) != version:
                raise FileExistsError("cleanup_changed")
            if action == "read":
                offset = data["offset"]
                source.seek(offset)
                content = source.read(READ_BYTES)
                if file_version(os.fstat(fd)) != file_version(before):
                    raise FileExistsError("cleanup_changed")
                if offset + len(content) < before.st_size:
                    # Keep UTF-8 text intact at page boundaries. Binary data is
                    # still returned byte for byte and can be displayed as hex.
                    try:
                        content.decode("utf-8")
                    except UnicodeDecodeError as error:
                        if error.reason == "unexpected end of data":
                            content = content[:error.start]
                end = offset + len(content)
                return {"content": base64.b64encode(content).decode("ascii"), "size": before.st_size,
                        "version": version, "next": end if end < before.st_size else None}
        if action != "delete" or data.get("version") != version:
            raise ValueError("Expected a confirmed deletion")
        if file_version(os.stat(name, dir_fd=parent, follow_symlinks=False)) != file_version(before):
            raise FileExistsError("cleanup_changed")
        check_deadline(until_ms)
        os.unlink(name, dir_fd=parent)
        os.fsync(parent)
        return {"deleted": True}

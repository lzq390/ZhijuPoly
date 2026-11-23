"""Offline M1 workspace data format. Only this module runs inside the volume helper."""

import ctypes
import errno
import fcntl
import hashlib
from heapq import merge
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tarfile


ROOT_DIRS = {"cache", "config", "files", "home", "records", "state"}
SKELETON = {".", *ROOT_DIRS, "files/general", "files/projects"}
CHUNK = 1024 * 1024
ZERO_CHUNK = bytes(CHUNK)
LEGACY_BLOCK = 4096
ZERO_BLOCK = bytes(LEGACY_BLOCK)
LAYOUT_VERSION = "NEXPOLY.sparse.version"
RESERVATION_MAP = "NEXPOLY.ext4.unwritten.v1"
FIEMAP_IOCTL = 0xC020660B
FIEMAP_HEADER = struct.Struct("=QQIIII")
FIEMAP_EXTENT = struct.Struct("=QQQQQIIII")
FIEMAP_BATCH = 128
FIEMAP_SYNC, FIEMAP_LAST, FIEMAP_UNWRITTEN = 1, 1, 0x0800
KEEP_SIZE = 1


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


def data_extents(source, size):
    """Ask the filesystem which byte ranges contain data, including allocated zero bytes."""
    fd = source.fileno()
    position = 0
    ranges = []
    while position < size:
        try:
            try:
                start = os.lseek(fd, position, os.SEEK_DATA)
            except OSError as error:
                if error.errno == errno.ENXIO:
                    break  # The rest of the file is a hole.
                raise
            end = min(os.lseek(fd, start, os.SEEK_HOLE), size)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.EOPNOTSUPP, errno.ENOSYS):
                raise
            if os.fstat(fd).st_blocks * 512 < size:
                raise ValueError("Filesystem cannot identify sparse data extents for backup") from error
            return [(0, size)]
        if not position <= start < end:
            raise ValueError("Invalid filesystem data extent")
        ranges.append((start, end - start))
        position = end
    return ranges


def append_extent(ranges, start, length):
    if length > 0:
        if ranges and ranges[-1][0] + ranges[-1][1] == start:
            start, previous = ranges.pop()
            length += previous
        ranges.append((start, length))


def fiemap_layout(fd, logical_size):
    """Read Linux FIEMAP through the last extent, including reservations beyond EOF."""
    data, unwritten = [], []
    start = 0
    while True:
        buffer = bytearray(FIEMAP_HEADER.size + FIEMAP_BATCH * FIEMAP_EXTENT.size)
        FIEMAP_HEADER.pack_into(buffer, 0, start, (2**64 - 1) - start, FIEMAP_SYNC, 0, FIEMAP_BATCH, 0)
        try:
            fcntl.ioctl(fd, FIEMAP_IOCTL, buffer, True)
        except OSError as error:
            if start == 0 and error.errno in (errno.EINVAL, errno.ENOTTY, errno.EOPNOTSUPP, errno.ENOSYS):
                return None
            raise
        mapped = FIEMAP_HEADER.unpack_from(buffer)[3]
        if mapped > FIEMAP_BATCH:
            raise ValueError("Invalid number of filesystem extents")
        if not mapped:
            break
        for index in range(mapped):
            row = FIEMAP_EXTENT.unpack_from(buffer, FIEMAP_HEADER.size + index * FIEMAP_EXTENT.size)
            offset, length, flags = row[0], row[2], row[5]
            if offset < start or not length or offset + length > 2**63 - 1:
                raise ValueError("Unexpected filesystem extent")
            if flags & (0x0002 | 0x0004):  # UNKNOWN or DELALLOC even after synchronous mapping.
                raise ValueError("Filesystem allocation is not yet stable")
            if flags & FIEMAP_UNWRITTEN:
                append_extent(unwritten, offset, length)
            elif offset < logical_size:
                append_extent(data, offset, min(length, logical_size - offset))
            start = offset + length
        if flags & FIEMAP_LAST:
            break
    return data, unwritten


def reservation_extents(member):
    raw = member.pax_headers.get(RESERVATION_MAP)
    if raw is None:
        return []
    if member.type != tarfile.REGTYPE or member.name == ".m1-owner":
        raise ValueError("Unexpected non-user file reservation")
    values = raw.split(",")
    if len(values) % 2 or not all(value.isascii() and value.isdecimal() for value in values):
        raise ValueError("Malformed file reservation map")
    ranges = []
    end = 0
    for i in range(0, len(values), 2):
        start, length = int(values[i]), int(values[i + 1])
        if start < end or length == 0 or start + length > 2**63 - 1:
            raise ValueError("Invalid file reservation extent")
        append_extent(ranges, start, length)
        end = start + length
    return ranges


def allocated_ranges(data, reservations):
    """Ordered union for disk admission; a byte cannot contain both data and unwritten space."""
    ranges = []
    for start, length in merge(data, reservations):
        if not length:
            continue
        if ranges and start < ranges[-1][0] + ranges[-1][1]:
            raise ValueError("Written data overlaps an unwritten reservation")
        append_extent(ranges, start, length)
    return ranges


def restore_reservations(sink, logical_size, reservations):
    sink.flush()
    fd = sink.fileno()
    allocate = ctypes.CDLL(None, use_errno=True).fallocate
    allocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64]
    for start, length in reservations:
        if allocate(fd, KEEP_SIZE, start, length) != 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))
    actual = fiemap_layout(fd, logical_size)
    if actual is None or actual[1] != reservations or os.fstat(fd).st_size != logical_size:
        raise ValueError("Restored unwritten extents or file length differs from the archive")


class PackedExtents(io.RawIOBase):
    """Let tarfile stream just the data extents without a second copy on disk."""
    def __init__(self, source, extents):
        self.source = source
        self.extents = iter(extents)
        self.remaining = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        if not self.remaining:
            start, self.remaining = next(self.extents, (0, 0))
            if not self.remaining:
                return 0
            self.source.seek(start)
        count = self.source.readinto(memoryview(buffer)[:self.remaining])
        if not count:
            raise ValueError("Workspace file changed while packing an extent")
        self.remaining -= count
        return count


def add_regular(archive, member, source):
    logical_size = member.size
    layout = fiemap_layout(source.fileno(), logical_size)
    if layout is None:
        ranges, reservations = data_extents(source, logical_size), []
        block = os.fstatvfs(source.fileno()).f_frsize
        data_allocation = sum(((start + length + block - 1) // block - start // block) * block
                              for start, length in ranges)
        if os.fstat(source.fileno()).st_blocks * 512 > data_allocation:
            raise ValueError("Cannot identify reserved space on this source filesystem")
    else:
        ranges, reservations = layout
    if reservations:
        member.pax_headers[RESERVATION_MAP] = ",".join(str(n) for extent in reservations for n in extent)
        reservation_extents(member)
    stored_size = sum(length for _, length in ranges)
    if stored_size == logical_size:
        # Some filesystems accept SEEK_DATA/SEEK_HOLE but report a single
        # dense extent for everything. Never silently flatten evident holes.
        if layout is None and os.fstat(source.fileno()).st_blocks * 512 < logical_size:
            raise ValueError("Filesystem did not identify sparse data extents for backup")
        source.seek(0)
        archive.addfile(member, source)
        return
    # GNU PAX sparse 0.1 keeps the physical tar payload compact and lets other
    # standard sparse-aware tar readers extract the same logical file.
    sparse_map = [*ranges, (logical_size, 0)]
    member.pax_headers.update({
        "GNU.sparse.size": str(logical_size),
        "GNU.sparse.numblocks": str(len(sparse_map)),
        "GNU.sparse.name": member.name,
        "GNU.sparse.map": ",".join(str(n) for extent in sparse_map for n in extent),
    })
    member.size = stored_size
    with io.BufferedReader(PackedExtents(source, ranges)) as packed:
        archive.addfile(member, packed)


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
            if name == ".":
                member.pax_headers[LAYOUT_VERSION] = "1"
            archive.addfile(member)
        else:
            member.size = info.st_size
            with path.open("rb", buffering=0) as source:
                if name == ".m1-owner":
                    if source.read(128) != workspace.encode():
                        raise ValueError("Workspace identity marker mismatch")
                    source.seek(0)
                add_regular(archive, member, source)
                observed = os.fstat(source.fileno())
                if (observed.st_dev, observed.st_ino, observed.st_ctime_ns, observed.st_size) != (
                    info.st_dev, info.st_ino, info.st_ctime_ns, info.st_size
                ):
                    raise ValueError("Workspace file changed during backup")


def hash_entry(tree_hash, name, directory, mode, owner, group, size, checksum):
    record = [name, directory, mode, owner, group, size, checksum]
    tree_hash.update(json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode() + b"\n")


class WorkspaceTarInfo(tarfile.TarInfo):
    # tarfile exposes the logical member size after PAX parsing. Retain the
    # underlying header length so we can validate the entire sparse payload.
    def _apply_pax_info(self, pax_headers, encoding, errors):
        self.header_size = self.size
        super()._apply_pax_info(pax_headers, encoding, errors)


def file_layout(archive, member):
    headers = member.pax_headers
    if member.sparse is None:
        if any(key.startswith("GNU.sparse.") for key in headers):
            raise ValueError("Incomplete sparse member header")
        return member.size, [(0, member.size)] if member.size else []
    if (member.type != tarfile.REGTYPE or member.name == ".m1-owner"
            or "GNU.sparse.map" not in headers or "GNU.sparse.size" not in headers):
        raise ValueError("Unexpected sparse member format")
    logical = int(headers["GNU.sparse.size"])
    packed = int(headers.get("size", member.header_size))
    ranges = member.sparse
    if (not 0 < logical <= 2**63 - 1 or packed < 0 or len(ranges) != int(headers.get("GNU.sparse.numblocks", -1))
            or headers["GNU.sparse.map"].count(",") + 1 != 2 * len(ranges)):
        raise ValueError("Invalid sparse map size")
    cursor = stored = 0
    for index, (start, length) in enumerate(ranges):
        if length == 0 and index == len(ranges) - 1 and start == logical:
            break  # GNU tar's optional EOF entry, also used for an all-hole file.
        if start < cursor or length <= 0 or start + length > logical:
            raise ValueError("Invalid sparse data extent")
        cursor = start + length
        stored += length
    if stored != packed:
        raise ValueError("Sparse map does not match stored payload size")
    # A PAX size override, when present, describes the packed payload on disk.
    # The next tar member begins after this payload and its block padding.
    archive.offset = member.offset_data + (packed + 511) // 512 * 512
    return logical, ranges


def legacy_data_runs(block):
    """Yield runs of 4 KiB blocks containing data. The historical tar has no extent map."""
    if len(block) == CHUNK and block == ZERO_CHUNK:
        return
    first = None
    for offset in range(0, len(block), LEGACY_BLOCK):
        length = min(len(block) - offset, LEGACY_BLOCK)
        if block.startswith(ZERO_BLOCK[:length], offset):
            if first is not None:
                yield first, offset
                first = None
        elif first is None:
            first = offset
    if first is not None:
        yield first, len(block)


def consume_file(archive, member, logical_size, ranges, sink, workspace, reconstruct, measure):
    checksum = hashlib.sha256()
    cursor = 0
    # Only the capacity calculation needs a recovered map. The write path
    # streams reconstructed extents directly to disk with bounded memory.
    recovered = [] if reconstruct and measure else None
    for start, length in ranges:
        hash_hole(checksum, start - cursor)
        cursor = start
        remaining = length
        while remaining:
            count = min(CHUNK, remaining)
            block = archive.fileobj.read(count)
            if len(block) != count:
                raise ValueError("Incomplete workspace archive")
            if member.name == ".m1-owner" and block != workspace.encode():
                raise ValueError("Workspace identity marker mismatch")
            checksum.update(block)
            if sink or recovered is not None:
                runs = legacy_data_runs(block) if reconstruct else [(0, count)]
                for left, right in runs:
                    begin = cursor + left
                    size = right - left
                    if sink:
                        sink.seek(begin)
                        sink.write(memoryview(block)[left:right])
                    if recovered is not None:
                        append_extent(recovered, begin, size)
            cursor += count
            remaining -= count
    hash_hole(checksum, logical_size - cursor)
    if sink:
        sink.truncate(logical_size)
    return checksum.hexdigest(), ranges if recovered is None else recovered


def hash_hole(checksum, size):
    while size >= CHUNK:
        checksum.update(ZERO_CHUNK)
        size -= CHUNK
    if size:
        checksum.update(memoryview(ZERO_CHUNK)[:size])


def process_archive(stream, uid, workspace, target=None, *, recover_legacy_zeros=False, allocation=None):
    """Validate, optionally restore, and report per-member physical data ranges to a capacity check."""
    tree_hash = hashlib.sha256()
    files = size = 0
    legacy = True
    seen, directories, metadata = set(), set(), []
    if target is not None and any(target.iterdir()):
        raise ValueError("Restore requires an empty volume")
    with tarfile.open(fileobj=stream, mode="r|", tarinfo=WorkspaceTarInfo) as archive:
        for member in archive:
            name = member.name
            folder = member.isdir()
            if (
                not (folder or member.type in {tarfile.REGTYPE, tarfile.AREGTYPE})
                or member.size < 0
                or (folder and (member.size or member.sparse is not None))
            ):
                raise ValueError("Unsupported archive entry")
            validate_entry(name, folder, member.mode, member.uid, member.gid, uid)
            if LAYOUT_VERSION in member.pax_headers:
                if name != "." or member.pax_headers[LAYOUT_VERSION] != "1":
                    raise ValueError("Unknown or misplaced workspace layout version")
                legacy = False
            if name in seen or (name != "." and str(Path(name).parent) not in directories):
                raise ValueError("Duplicate entry or invalid archive order")
            seen.add(name)
            dest = target / name if target is not None else None
            checksum = None
            logical_size = 0
            ranges = []
            reservations = reservation_extents(member)
            if folder:
                directories.add(name)
                if dest is not None and name != ".":
                    dest.mkdir(mode=0o700)
            else:
                logical_size, ranges = file_layout(archive, member)
                if reservations:
                    allocated_ranges(ranges, reservations)  # Validate disjointness before creating the file.
                if name == ".m1-owner" and logical_size != len(workspace):
                    raise ValueError("Invalid workspace marker size")
                reconstruct = (recover_legacy_zeros and legacy and member.sparse is None
                               and not reservations and name != ".m1-owner")
                sink = dest.open("xb") if dest is not None else None
                try:
                    checksum, ranges = consume_file(
                        archive, member, logical_size, ranges, sink, workspace, reconstruct, allocation is not None
                    )
                    if sink and reservations:
                        restore_reservations(sink, logical_size, reservations)
                finally:
                    if sink:
                        sink.close()
                files += 1
                size += logical_size
            if allocation is not None:
                allocation(member, allocated_ranges(ranges, reservations) if reservations else ranges, legacy)
            if dest is not None:
                metadata.append((dest, member))
            hash_entry(tree_hash, name, folder, member.mode, member.uid, member.gid, logical_size, checksum)
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

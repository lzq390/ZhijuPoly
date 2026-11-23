"""Quota-aware backup and restore for offline dev-m1 ext4 test workspaces."""

from contextlib import ExitStack, contextmanager
import argparse
import errno
import fcntl
import hashlib
from importlib import import_module
import json
import os
import shutil
from pathlib import Path
import stat
import sys
import tarfile
from tempfile import TemporaryDirectory

from openscience_m5_archive import (
    add_tree,
    entries,
    hash_entry,
    process_archive,
    validate_entry,
)
from openscience_m5_backup import (
    BACKUP_FREE_FLOOR,
    available_bytes,
    destination_path,
    digest,
    extent_bytes,
    private_dir,
    private_file,
    publish,
    require_space,
)

# In the offline guest all modules reside together on the read-only input image.
backend_dir = Path(__file__).resolve().parents[1] / "ops/openscience-per-user-dev"
if backend_dir.is_dir():
    sys.path.insert(0, str(backend_dir))
quota = import_module("quota")


FORMAT = "openscience-m5-ext4-workspace-v1"


def inventory(root, binding):
    # Produce the same canonical tree digest used by the earlier M5 restore.
    tree = hashlib.sha256()
    files = folders = size = 0
    for path, name in entries(root):
        info = path.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError("Expected a single-link regular file or a directory")
        mode = stat.S_IMODE(info.st_mode)
        validate_entry(name, directory, mode, info.st_uid, info.st_gid, binding["uid"])
        checksum = None
        length = 0
        if directory:
            folders += 1
        else:
            length = info.st_size
            with path.open("rb") as src:
                checksum = digest(src)
            files += 1
            size += length
        hash_entry(
            tree, name, directory, mode, info.st_uid, info.st_gid, length, checksum
        )
    return {
        "tree_sha256": tree.hexdigest(),
        "files": files,
        "directories": folders,
        "file_bytes": size,
    }


check_source_mount = quota.check_source_mount


# Deliberately small isolated fixture disk. Other callers use the deployment floor.
DRILL_BACKUP_FLOOR = 16 * 1024**2


class ArchiveBudget:
    """Size the identical PAX headers, sparse payloads and final record padding without copying data."""

    def __init__(self):
        self.size = 0

    def addfile(self, member, fileobj=None):
        self.size += len(member.tobuf(format=tarfile.PAX_FORMAT))
        self.size += (member.size + 511) // 512 * 512

    def padded_size(self):
        record = tarfile.RECORDSIZE
        return (self.size + 2 * tarfile.BLOCKSIZE + record - 1) // record * record


class ReservedArchive:
    def __init__(self, file, limit):
        self.file = file
        self.limit = limit

    def write(self, data):
        if self.file.tell() + len(data) > self.limit:
            raise ValueError("Offline source exceeded its reserved archive size")
        return self.file.write(data)


@contextmanager
def disk_admission(directory):
    # Lock the filesystem root, so concurrent owner/generation writers using
    # different subdirectories on this dedicated disk share admission.
    root = directory.resolve()
    device = root.stat().st_dev
    while root != root.parent and root.parent.stat().st_dev == device:
        root = root.parent
    fd = os.open(root, os.O_DIRECTORY | os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def backup_staging(output, policy):
    # Same-name destinations in separate private directories have independent stages.
    # Bind the receipt to the source as well, so a new owner cannot reclaim a prior run.
    token = hashlib.sha256(os.fsencode(output.name)).hexdigest()
    pending = output.parent / (".m5-quota-pending-" + token)
    receipt = json.dumps({"format": "m5-quota-backup-stage-v1", "target": output.name, "quota": policy},
                         sort_keys=True).encode()
    return pending, receipt


def reclaim_backup_stage(pending, receipt):
    if not os.path.lexists(pending):
        return
    private_dir(pending)
    check_source_mount(pending, pending.parent)
    with private_file(pending / "intent.json") as file:
        if file.read(len(receipt) + 1) != receipt:
            raise ValueError("Unrecognized quota backup staging receipt")
    if {p.name for p in pending.iterdir()} - {"intent.json", "package"}:
        raise ValueError("Unexpected entries in quota backup staging directory")
    package = pending / "package"
    if os.path.lexists(package):
        private_dir(package)
        for path in package.iterdir():
            if path.name not in ("workspace.tar", "manifest.json"):
                raise ValueError("Unexpected file in staged quota backup")
            with private_file(path) as file:
                if os.fstat(file.fileno()).st_nlink != 1:
                    raise ValueError("Unexpected hard link in staged quota backup")
    shutil.rmtree(pending)


def claim_stage(pending, receipt):
    # Publish and fsync the claim before allocating user data. A crash before
    # this rename leaves at most a small control directory.
    with TemporaryDirectory(dir=pending.parent, prefix=".m5-quota-claim-") as tmp:
        stage = Path(tmp) / "owned"
        stage.mkdir(mode=0o700)
        with (stage / "intent.json").open("xb") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(receipt)
            file.flush()
            os.fsync(file.fileno())
        fd = os.open(stage, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        publish(stage, pending)


def backup(mount, binding, bundle_dir, *, reserve_bytes=BACKUP_FREE_FLOOR):
    """Export quiescent source records and the enforced source UID policy as a single pinned package."""
    mount = Path(mount)
    output = destination_path(Path(bundle_dir))
    private_dir(output.parent)
    if output.parent.stat().st_dev == mount.stat().st_dev:
        raise ValueError("Use a separate filesystem for the test backup")
    if type(reserve_bytes) is not int or reserve_bytes <= 0:
        raise ValueError("Expected a positive backup filesystem reserve in bytes")
    source = mount / "workspace"
    check_source_mount(source, mount)
    policy = quota.snapshot(mount, binding)
    budget = ArchiveBudget()
    add_tree(source, budget, binding["uid"], binding["workspace"])
    required = budget.padded_size()
    # Budget the manifest and the handful of staging, directory and extent metadata blocks.
    metadata = 65536 + 8 * os.statvfs(output.parent).f_frsize
    with ExitStack() as stack:
        stack.enter_context(disk_admission(output.parent))
        destination_path(output)
        pending, receipt = backup_staging(output, policy)
        reclaim_backup_stage(pending, receipt)
        free_before = available_bytes(output.parent)
        require_space(free_before, required + metadata, reserve_bytes)
        claim_stage(pending, receipt)
        stack.callback(shutil.rmtree, pending)
        stage = pending / "package"
        stage.mkdir(mode=0o700)
        archive_fd = os.open(stage / "workspace.tar", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        with os.fdopen(archive_fd, "w+b") as archive:
            os.posix_fallocate(archive.fileno(), 0, required)
            require_space(available_bytes(output.parent), metadata, reserve_bytes)
            with tarfile.open(
                fileobj=ReservedArchive(archive, required), mode="w|", format=tarfile.PAX_FORMAT
            ) as writer:
                add_tree(source, writer, binding["uid"], binding["workspace"])
            if archive.tell() != required:
                raise ValueError("Offline source changed its reserved archive size")
            archive.flush()
            os.fsync(archive.fileno())
            archive.seek(0)
            archive_pin = digest(archive)
            archive.seek(0)
            tree = process_archive(archive, binding["uid"], binding["workspace"])
        if (
            tree != inventory(source, binding)
            or quota.snapshot(mount, binding) != policy
        ):
            raise ValueError("Source contents or kernel limits changed during backup")
        manifest = {
            "format": FORMAT,
            "quota": policy,
            "archive_sha256": archive_pin,
            "inventory": tree,
        }
        raw = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
        manifest_fd = os.open(stage / "manifest.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(manifest_fd, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        fd = os.open(stage, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        free_after = available_bytes(output.parent)
        require_space(free_after, os.statvfs(output.parent).f_frsize, reserve_bytes)
        publish(stage, output)
    return {
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "inventory": tree,
        "quota": policy,
        "capacity": {"archive_bytes": required, "reserve_bytes": reserve_bytes,
                     "free_before_bytes": free_before, "free_before_publish_bytes": free_after},
    }


@contextmanager
def restore_stage(mount, pin):
    # This path is private to one verified package and the target volume lock.
    # A killed extractor may have consumed UID quota. Reclaim only its unpublished tree.
    stage = mount / (".m5-quota-restore-" + pin)
    receipt = ("m5-quota-restore-stage-v1 " + pin).encode("ascii")
    interrupted = os.path.lexists(stage)
    if interrupted:
        private_dir(stage)
        check_source_mount(stage, mount)
        with private_file(stage / "intent.json") as file:
            if file.read(len(receipt) + 1) != receipt:
                raise ValueError("Unrecognized quota restore staging receipt")
        for child in stage.iterdir():
            if child.name == "intent.json":
                continue
            if not child.name.startswith("attempt-"):
                raise ValueError("Unexpected entry in quota restore staging directory")
            private_dir(child)
            if {path.name for path in child.iterdir()} - {"workspace"}:
                raise ValueError("Unexpected entry in interrupted quota restore attempt")
        shutil.rmtree(stage)
    claim_stage(stage, receipt)
    try:
        yield stage, interrupted
    finally:
        shutil.rmtree(stage)


def restore(mount, binding, bundle_dir, trusted_manifest_sha256):
    """Restore to a new workspace. Publish only after checking ownership, kernel quota and every file."""
    mount, package = Path(mount), Path(bundle_dir)
    private_dir(package)
    with ExitStack() as streams:
        with private_file(package / "manifest.json") as file:
            raw = file.read(65537)
        if (
            len(raw) > 65536
            or hashlib.sha256(raw).hexdigest() != trusted_manifest_sha256
        ):
            raise ValueError("Unexpected quota package manifest")
        meta = json.loads(raw)
        if meta["format"] != FORMAT or any(
            meta["quota"]["identity"][k] != binding[k]
            for k in ("environment", "user_id", "workspace", "uid")
        ):
            raise ValueError("Quota package does not belong to the target binding")
        archive = streams.enter_context(private_file(package / "workspace.tar"))
        if os.fstat(archive.fileno()).st_dev == mount.stat().st_dev:
            raise ValueError("Use separate backup and restore filesystems")
        if digest(archive) != meta["archive_sha256"]:
            raise ValueError("Workspace archive does not match the quota package")
        block = os.statvfs(mount).f_frsize
        user_bytes = total_bytes = 0
        legacy = False

        def allocation(member, ranges, old_format):
            nonlocal legacy, user_bytes, total_bytes
            legacy = old_format
            if legacy:
                size = block if member.isdir() else extent_bytes(ranges, block)
                total_bytes += size
                if member.uid == binding["uid"]:
                    user_bytes += size

        archive.seek(0)
        if (
            process_archive(archive, binding["uid"], binding["workspace"], allocation=allocation)
            != meta["inventory"]
        ):
            raise ValueError("Unexpected archived workspace contents")
        guard = os.open(mount, os.O_RDONLY | os.O_DIRECTORY)
        streams.callback(os.close, guard)
        fcntl.flock(guard, fcntl.LOCK_EX)
        target = destination_path(mount / "workspace")
        policy = meta["quota"]
        effective = quota.restore(mount, binding, policy, quota.snapshot_digest(policy))
        staging, interrupted = streams.enter_context(restore_stage(mount, trusted_manifest_sha256))
        if interrupted:
            effective = quota.read(mount, binding)
        fs = os.statvfs(mount)
        recover_zeros = legacy and (
            user_bytes > effective["hard_bytes"] - effective["used_bytes"]
            or total_bytes > fs.f_bavail * fs.f_frsize
        )
        while True:
            archive.seek(0)
            with TemporaryDirectory(dir=staging, prefix="attempt-") as attempt:
                restored = Path(attempt) / "workspace"
                restored.mkdir(mode=0o700)
                try:
                    restored_tree = process_archive(
                        archive, binding["uid"], binding["workspace"], restored,
                        recover_legacy_zeros=recover_zeros,
                    )
                except OSError as error:
                    # Directory growth and extent metadata may exceed the estimate. The
                    # failed tree is private and removed before a single legacy retry.
                    if not legacy or recover_zeros or error.errno not in (errno.EDQUOT, errno.ENOSPC):
                        raise
                    recover_zeros = True
                    continue
                effective = quota.read(mount, binding)
                if effective["hard_bytes"] != policy["hard_bytes"]:
                    raise ValueError("Restored quota differs from backup")
                if effective["used_bytes"] > effective["hard_bytes"]:
                    # Extraction can run with privileges to reinstate owners.
                    # Still enforce the measured final hard limit before publication.
                    if legacy and not recover_zeros:
                        recover_zeros = True
                        continue
                    raise OSError(errno.EDQUOT, "Restored workspace exceeds its recovered quota")
                if restored_tree != meta["inventory"] or inventory(restored, binding) != restored_tree:
                    raise ValueError("Restored files differ from backup")
                publish(restored, target)
                result = {"inventory": restored_tree, "quota": effective}
                if recover_zeros:
                    result["legacy_zero_recovery"] = True
                return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mount", required=True, type=Path)
    parser.add_argument(
        "--binding", required=True, type=Path, help="Trusted dev-m1 owner binding JSON"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser(
        "backup", help="Export a quiescent workspace to another filesystem"
    )
    save.add_argument("--package", required=True, type=Path)
    save.add_argument("--reserve-bytes", type=int, default=BACKUP_FREE_FLOOR,
                      help="Minimum available bytes to retain on the dedicated backup filesystem")
    recover = commands.add_parser(
        "restore", help="Publish the verified workspace on a fresh quota volume"
    )
    recover.add_argument("--package", required=True, type=Path)
    recover.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    binding = json.loads(args.binding.read_bytes())
    if args.command == "backup":
        result = backup(args.mount, binding, args.package, reserve_bytes=args.reserve_bytes)
    else:
        result = restore(args.mount, binding, args.package, args.manifest_sha256)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

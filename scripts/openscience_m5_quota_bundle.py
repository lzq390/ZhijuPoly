"""Quota-aware backup and restore for offline dev-m1 ext4 test workspaces."""

from contextlib import ExitStack
import argparse
import hashlib
from importlib import import_module
import json
import os
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
    destination_path,
    digest,
    private_dir,
    private_file,
    publish,
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


def backup(mount, binding, bundle_dir):
    """Export quiescent source records and the enforced source UID policy as a single pinned package."""
    mount = Path(mount)
    output = destination_path(Path(bundle_dir))
    private_dir(output.parent)
    if output.parent.stat().st_dev == mount.stat().st_dev:
        raise ValueError("Use a separate filesystem for the test backup")
    source = mount / "workspace"
    check_source_mount(source, mount)
    policy = quota.snapshot(mount, binding)
    with TemporaryDirectory(dir=output.parent, prefix=".m5-quota-pack-") as tmp:
        stage = Path(tmp) / "package"
        stage.mkdir(mode=0o700)
        with (stage / "workspace.tar").open("x+b") as archive:
            os.fchmod(archive.fileno(), 0o600)
            with tarfile.open(
                fileobj=archive, mode="w|", format=tarfile.PAX_FORMAT
            ) as writer:
                add_tree(source, writer, binding["uid"], binding["workspace"])
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
        with (stage / "manifest.json").open("xb") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        fd = os.open(stage, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        publish(stage, output)
    return {
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "inventory": tree,
        "quota": policy,
    }


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
        archive.seek(0)
        if (
            process_archive(archive, binding["uid"], binding["workspace"])
            != meta["inventory"]
        ):
            raise ValueError("Unexpected archived workspace contents")
        archive.seek(0)
        target = destination_path(mount / "workspace")
        policy = meta["quota"]
        quota.restore(mount, binding, policy, quota.snapshot_digest(policy))
        with TemporaryDirectory(dir=mount, prefix=".m5-quota-restore-") as tmp:
            restored = Path(tmp) / "workspace"
            restored.mkdir(mode=0o700)
            restored_tree = process_archive(
                archive, binding["uid"], binding["workspace"], restored
            )
            if (
                restored_tree != meta["inventory"]
                or inventory(restored, binding) != restored_tree
            ):
                raise ValueError("Restored files differ from backup")
            effective = quota.read(mount, binding)
            if (
                effective["hard_bytes"] != policy["hard_bytes"]
                or effective["used_bytes"] > effective["hard_bytes"]
            ):
                raise ValueError("Restored workspace exceeds its recovered quota")
            publish(restored, target)
    return {"inventory": restored_tree, "quota": effective}


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
    recover = commands.add_parser(
        "restore", help="Publish the verified workspace on a fresh quota volume"
    )
    recover.add_argument("--package", required=True, type=Path)
    recover.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    binding = json.loads(args.binding.read_bytes())
    if args.command == "backup":
        result = backup(args.mount, binding, args.package)
    else:
        result = restore(args.mount, binding, args.package, args.manifest_sha256)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

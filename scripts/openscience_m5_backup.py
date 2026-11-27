"""Archive an offline dev workspace, or restore pinned archives into a new development registry."""

import argparse
from contextlib import ExitStack, contextmanager
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace

import openscience_per_user_dev as m1
from openscience_m5_archive import (
    CHUNK,
    WorkspaceTarInfo,
    allocated_ranges,
    file_layout,
    process_archive,
    reservation_extents,
)


HELPER = Path(__file__).with_name("openscience_m5_archive.py").resolve()
MIB = 1024 * 1024
BACKUP_FREE_FLOOR = 16 * 1024 * MIB
STAMP = "org.nexpoly.m5.restore"
SCOPE = "workspace-records-and-files-v1"
LAYOUT = {
    "content": ["records/**", "files/**"],
    "excluded": ["files/.runtime/**", "home/**", "cache/**", "state/**", "config/**"],
    "metadata": [".m1-owner", "root", "empty standard directories"],
}


def digest(stream):
    result = hashlib.sha256()
    while block := stream.read(CHUNK):
        result.update(block)
    return result.hexdigest()


def private_dir(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise m1.Problem("Expected an owned private directory (0700)")


def private_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise m1.Problem("Expected an owned private backup file (0600)")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def validate_owner(env, user):
    if not re.fullmatch(r"dev-[a-z0-9][a-z0-9-]{0,39}", env) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", user
    ):
        raise m1.Problem("Expected a development environment and stable user identifier")


def publish(staging, destination):
    # Unlike os.rename, never replace a competing empty destination directory.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(-100, os.fsencode(staging), -100, os.fsencode(destination), 1) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))
    fd = os.open(destination.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def destination_path(path):
    if path.is_symlink() or path.exists():
        raise m1.Problem("Destination must be new; existing content is never replaced")
    return path.parent.resolve(strict=True) / path.name


@contextmanager
def offline_registry(root):
    private_dir(root)
    # Read-only opens on the source, including the pre-existing manager lock.
    with ExitStack() as stack:
        if (root / "service.lock").exists():
            locks = ["service.lock", "supervisor.lock"]
        else:
            locks = ["supervisor.lock"]
        for name in locks:
            guard = stack.enter_context((root / name).open("rb"))
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise m1.Problem("Stop the supervisor and other registry operations first") from exc
        yield m1.Registry(root)


def helper(workspace, operation):
    writable = operation == "unpack"
    mount = f"type=volume,src={workspace.volume},dst=/data,volume-nocopy" + ("" if writable else ",readonly")
    permissions = (
        ["--cap-add=CHOWN", "--cap-add=DAC_OVERRIDE", "--cap-add=FOWNER"]
        if writable
        else ["--user", workspace.identity]
    )
    return [
        "docker",
        "run",
        "--rm",
        "-i",
        "--pull=never",
        "--read-only",
        "--network=none",
        "--log-driver=none",
        "--pids-limit=32",
        "--memory=192m",
        "--cpus=1",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        *permissions,
        "--mount",
        mount,
        "--mount",
        f"type=bind,src={HELPER},dst=/m5.py,readonly",
        m1.PYTHON,
        "python",
        "-IB",
        "/m5.py",
        operation,
        workspace.uid,
        workspace.row["id"],
    ]


def is_unmounted(workspace):
    if m1.docker("ps", "-aq", "--filter", "volume=" + workspace.volume):
        raise m1.Problem("A container still references the workspace volume")


def physical_disk(device, sys_block=Path("/sys/dev/block")):
    if not re.fullmatch(r"[0-9]+:[0-9]+", device):
        raise m1.Problem("Unrecognized filesystem block device")
    try:
        node = (sys_block / device).resolve(strict=True)
        if (node / "partition").is_file():
            node = node.parent
        # A stacked, virtual or memory-backed target cannot establish an
        # independent persistent disk for this offline recovery workflow.
        if "virtual" in node.parts or not (node / "slaves").is_dir() or any((node / "slaves").iterdir()):
            raise m1.Problem("Only direct persistent block devices are supported")
        hardware = (node / "device").resolve(strict=True)
        disk_id = (node / "dev").read_text().strip()
        if not re.fullmatch(r"[0-9]+:[0-9]+", disk_id):
            raise ValueError("Unexpected physical disk identity")
        return {"name": node.name, "device": disk_id}, hardware
    except (OSError, ValueError) as exc:
        raise m1.Problem("Could not verify a direct persistent physical disk") from exc


def separate_disks(source, destination, sys_block=Path("/sys/dev/block")):
    first, source_hardware = physical_disk(source, sys_block)
    second, target_hardware = physical_disk(destination, sys_block)
    if first["device"] == second["device"] or source_hardware == target_hardware:
        raise m1.Problem("Select a backup directory on a separate physical disk")
    return first, second


def available_bytes(path):
    fs = os.statvfs(path)
    return fs.f_bavail * fs.f_frsize


def require_space(available, requested, floor):
    if available < requested + floor:
        raise m1.Problem(
            f"Insufficient free space in bytes: available={available}, requested={requested}, floor={floor}"
        )


def stream_backup(command, archive, destination):
    require_space(available_bytes(destination), CHUNK, BACKUP_FREE_FLOOR)
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE) as proc:
        with proc.stdout as stream:
            while block := stream.read(CHUNK):
                require_space(available_bytes(destination), len(block), BACKUP_FREE_FLOOR)
                archive.write(block)
        if proc.wait():
            raise m1.Problem("Could not read the offline workspace volume")


def backup(registry_path, environment, user, output):
    validate_owner(environment, user)
    destination = destination_path(output)
    private_dir(destination.parent)
    with offline_registry(registry_path) as registry:
        workspace = registry.workspace(environment, user)
        if workspace.resource("backend") or workspace.resource("fixture") or workspace.row["leases"]:
            raise m1.Problem("Recycle workspace processes and release all leases first")
        if not workspace.row["initialized"] or not workspace.resource("volume"):
            raise m1.Problem("Expected an initialized and owned source volume")
        is_unmounted(workspace)
        source_device = subprocess.check_output(helper(workspace, "device"), text=True).strip()
        target_dev = destination.parent.stat().st_dev
        target_device = f"{os.major(target_dev)}:{os.minor(target_dev)}"
        source_disk, archive_disk = separate_disks(source_device, target_device)
        stage = Path(tempfile.mkdtemp(dir=destination.parent, prefix=".m5-backup-"))
        try:
            with (stage / "workspace.tar").open("x+b") as archive:
                os.fchmod(archive.fileno(), 0o600)
                stream_backup(helper(workspace, "pack"), archive, destination.parent)
                archive.flush()
                os.fsync(archive.fileno())
                archive.seek(0)
                archive_hash = digest(archive)
                archive.seek(0)
                inventory = process_archive(archive, int(workspace.uid), workspace.row["id"])
            manifest = {
                "format": SCOPE,
                "scope": LAYOUT,
                "environment": environment,
                "user_id": user,
                "workspace": workspace.row["id"],
                "uid": workspace.row["uid"],
                "generation": workspace.row["generation"],
                "source_deployment": workspace.deployment,
                "source_volume": workspace.volume,
                "source_device": source_device,
                "archive_device": target_device,
                "source_disk": source_disk,
                "archive_disk": archive_disk,
                "registry_settings": {
                    k: registry.data[k] for k in ("template_version", "image", "idle_seconds", "max_active")
                },
                "capacity": registry.capacity(),
                "backup_free_floor_bytes": BACKUP_FREE_FLOOR,
                "archive_sha256": archive_hash,
                "inventory": inventory,
            }
            m1.write_atomic(
                stage / "manifest.json",
                (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
                0o600,
            )
            publish(stage, destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return {
        "user_id": user,
        "manifest_sha256": hashlib.sha256((destination / "manifest.json").read_bytes()).hexdigest(),
        **inventory,
        "source_device": source_device,
        "archive_device": target_device,
        "source_disk": source_disk,
        "archive_disk": archive_disk,
        "capacity": manifest["capacity"],
        "backup_free_floor_bytes": BACKUP_FREE_FLOOR,
    }


def verify_package(stack, environment, owner, directory, pinned_hash):
    validate_owner(environment, owner)
    private_dir(directory)
    with private_file(directory / "manifest.json") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != pinned_hash:
        raise m1.Problem("Manifest does not match supplied trusted digest")
    manifest = json.loads(raw)
    if (
        manifest["format"] != SCOPE
        or manifest["scope"] != LAYOUT
        or manifest["environment"] != environment
        or manifest["user_id"] != owner
    ):
        raise m1.Problem("Backup scope or requested owner differs")
    if not re.fullmatch("[a-f0-9]{32}", manifest["workspace"]) or not re.fullmatch(
        "[a-f0-9]{32}", manifest["source_deployment"]
    ):
        raise m1.Problem("Invalid workspace or deployment")
    if (
        not isinstance(manifest["uid"], int)
        or not 210000 <= manifest["uid"] < 100210000
        or not isinstance(manifest["generation"], int)
        or manifest["generation"] < 1
    ):
        raise m1.Problem("Invalid backup ownership or generation")
    if (
        manifest["registry_settings"]["template_version"] != m1.VERSION
        or manifest["source_volume"]
        != f"nexpoly-m1-{manifest['source_deployment'][:12]}-{manifest['workspace']}-data"
    ):
        raise m1.Problem("Incompatible template or source volume")
    m1.validated_capacity(manifest.get("capacity"))
    archive = stack.enter_context(private_file(directory / "workspace.tar"))
    if digest(archive) != manifest["archive_sha256"]:
        raise m1.Problem("Archive digest differs")
    archive.seek(0)
    if process_archive(archive, manifest["uid"], manifest["workspace"]) != manifest["inventory"]:
        raise m1.Problem("Archive inventory differs")
    archive.seek(0)
    return manifest, archive


def restored_inventory(workspace):
    with subprocess.Popen(
        helper(workspace, "pack"), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE
    ) as proc:
        with proc.stdout as result:
            inventory = process_archive(result, int(workspace.uid), workspace.row["id"])
            while result.read(CHUNK):
                pass
        if proc.wait():
            raise m1.Problem("Could not reread restored volume")
    return inventory


def delete_owned_empty_instance_volume(workspace, stamp):
    if workspace.resource("volume"):
        is_unmounted(workspace)
        actual = m1.docker(
            "volume", "inspect", "--format", '{{index .Labels "' + STAMP + '"}}', workspace.volume
        )
        if actual != stamp:
            raise m1.Problem("Refusing cleanup of an unrelated volume")
        m1.docker("volume", "rm", workspace.volume)


def extent_bytes(ranges, block):
    """Count destination blocks touched by ordered, non-overlapping data ranges."""
    total = end_block = 0
    for offset, length in ranges:
        if length:
            end = (offset + length + block - 1) // block
            total += (end - max(offset // block, end_block)) * block
            end_block = end
    return total


def restore_space_required(verified):
    block = 4096  # Development Docker volume's ext4 block size.
    required = 0
    for item, archive in verified:
        # Keep the existing archive and metadata reserve. A sparse tar stores
        # only written data. A restored KEEP_SIZE reservation costs real blocks
        # even beyond EOF; data and reservations can also share a block.
        required += os.fstat(archive.fileno()).st_size
        required += (item["inventory"]["files"] + item["inventory"]["directories"]) * block
        try:
            archive.seek(0)
            with tarfile.open(fileobj=archive, mode="r:", tarinfo=WorkspaceTarInfo) as reader:
                for member in reader:
                    reservations = reservation_extents(member)
                    if member.sparse is None and not reservations:
                        continue
                    _, data = file_layout(reader, member)
                    allocated = allocated_ranges(data, reservations)
                    required += extent_bytes(allocated, block) - sum(length for _, length in data)
        finally:
            archive.seek(0)  # The same pinned descriptor feeds the unpack helper.
    return required


def restore(destination, environment, packages):
    if not packages:
        raise m1.Problem("Provide at least one owner archive")
    # Verify every owner, archive and trusted digest before touching a registry or volume.
    with ExitStack() as stack:
        verified = [verify_package(stack, environment, user, Path(path), pin) for user, path, pin in packages]
        first = verified[0][0]
        for key in ("workspace", "uid", "user_id"):
            if len({item[key] for item, _ in verified}) != len(verified):
                raise m1.Problem("Duplicate owner or workspace in restore set")
        if any(
            item["registry_settings"] != first["registry_settings"]
            or item["source_deployment"] != first["source_deployment"]
            or item["capacity"] != first["capacity"]
            for item, _ in verified
        ):
            raise m1.Problem("Restore set must come from one development deployment and template")
        destination = destination_path(destination)
        stage = Path(tempfile.mkdtemp(dir=destination.parent, prefix=".m5-restore-"))
        owned = []
        try:
            with m1.locked_registry(stage, True) as registry:
                registry.initialize(SimpleNamespace(**first["registry_settings"]))
                registry.set_capacity(**first["capacity"])
                admission_workspace = m1.Workspace(
                    registry,
                    environment,
                    first["user_id"],
                    {"id": first["workspace"], "uid": first["uid"], "initialized": False},
                )
                available = admission_workspace.volume_free_mib() * MIB
                required = restore_space_required(verified)
                floor = first["capacity"]["volume_free_floor_mib"] * MIB
                require_space(available, required, floor)
                stamp = registry.data["deployment"]
                if stamp[:12] == first["source_deployment"][:12]:
                    raise m1.Problem("Unexpected source deployment collision")
                owners = {}
                for manifest, archive in verified:
                    owner = manifest["user_id"]
                    row = {
                        "id": manifest["workspace"],
                        "uid": manifest["uid"],
                        "generation": manifest["generation"],
                        "initialized": True,
                        "last_activity": 0,
                        "leases": [],
                        "failed_starts": 0,
                        "retry_after": 0,
                    }
                    workspace = m1.Workspace(registry, environment, owner, row)
                    if (
                        workspace.resource("volume")
                        or workspace.resource("backend")
                        or workspace.resource("fixture")
                    ):
                        raise m1.Problem("Restore target resources already exist")
                    m1.docker(
                        "volume",
                        "create",
                        *workspace.label_flags("volume"),
                        "--label",
                        STAMP + "=" + stamp,
                        workspace.volume,
                    )
                    owned.append(workspace)
                    if not workspace.resource("volume"):
                        raise m1.Problem("Restored volume creation failed")
                    is_unmounted(workspace)
                    result = subprocess.run(
                        helper(workspace, "unpack"), stdin=archive, stdout=subprocess.PIPE, check=True
                    )
                    if (
                        json.loads(result.stdout) != manifest["inventory"]
                        or restored_inventory(workspace) != manifest["inventory"]
                    ):
                        raise m1.Problem("Restored file inventory differs from backup")
                    workspace.helper(m1.CHECK_VOLUME, row["id"])
                    registry.data["workspaces"][environment + "/" + owner] = row
                    owners[owner] = {
                        "workspace": row["id"],
                        "uid": row["uid"],
                        "generation": row["generation"],
                        "volume": workspace.volume,
                        **manifest["inventory"],
                    }
                registry.save()
            publish(stage, destination)
        except BaseException:
            # Clean only this fresh deployment, never a prior restore, source volume or occupied target.
            if stage.exists():
                for workspace in reversed(owned):
                    delete_owned_empty_instance_volume(workspace, stamp)
                shutil.rmtree(stage)
            raise
        return {
            "registry": str(destination),
            "deployment": stamp,
            "owners": owners,
            "capacity": first["capacity"],
            "preflight_volume_free_bytes": available,
            "restore_reserved_bytes": required,
            "volume_free_floor_bytes": floor,
        }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cmds = cli.add_subparsers(dest="command", required=True)
    save = cmds.add_parser(
        "backup", help="Read a fully recycled local volume, write a new package on another device."
    )
    save.add_argument("--registry", type=Path, required=True)
    save.add_argument("--environment", required=True)
    save.add_argument("--user-id", required=True)
    save.add_argument("--output", type=Path, required=True)
    recover = cmds.add_parser(
        "restore", help="Validate the full set, then publish a new registry with new offline volumes."
    )
    recover.add_argument("--registry", type=Path, required=True)
    recover.add_argument("--environment", required=True)
    recover.add_argument(
        "--owner",
        nargs=3,
        action="append",
        required=True,
        metavar=("USER", "PACKAGE", "TRUSTED_MANIFEST_SHA256"),
    )
    args = cli.parse_args()
    os.umask(0o077)
    try:
        m1.local_engine()
        if args.command == "backup":
            result = backup(args.registry, args.environment, args.user_id, args.output)
        else:
            result = restore(args.registry, args.environment, args.owner)
        print(json.dumps(result))
    except (OSError, ValueError, KeyError, m1.Problem, subprocess.SubprocessError) as exc:
        print(f"M5 {args.command} failed: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

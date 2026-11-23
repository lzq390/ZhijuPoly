"""Inspect, fill and restore ext4 on the storage side of the M5 browser drill."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "ops/openscience-per-user-dev"
sys.path[:0] = [str(ROOT / "scripts"), str(OPS)]
from openscience_m5_quota_bundle import DRILL_BACKUP_FLOOR, backup, disk_admission, restore, inventory, quota  # noqa: E402
from openscience_m5_backup import available_bytes, digest, private_dir, private_file, publish, require_space  # noqa: E402

guest = runpy.run_path(str(OPS / "quota-guest.py"))
MOUNTS = guest["MOUNT_ROOT"]
RELEASE = Path("files/projects/same-project/m5-release.txt")


def require(ok, detail):
    if not ok:
        raise ValueError(detail)


def mount(phase, role):
    return MOUNTS / (phase + "-" + role)


def release_candidate(path):
    with path.open("xb") as stream:
        stream.write(b"M5 quota release file\n")
        stream.flush()
        subprocess.run(
            ["fallocate", "--keep-size", "--length", "2MiB", "--", str(path)],
            check=True,
        )
        os.fsync(stream.fileno())
    return guest["file_facts"](path)


def fill(owner):
    disk = mount("src", "a")
    ws = disk / "workspace"
    require((ws / RELEASE).is_file(), "Browser release candidate missing")
    before = quota.read(disk, owner)
    size = guest["run_as"](
        owner, guest["reserve_to_limit"], ws, guest["LIMIT"], before["used_bytes"]
    )
    after = quota.read(disk, owner)
    free = os.statvfs(ws)
    require(
        after["hard_bytes"] - after["used_bytes"] < free.f_frsize and free.f_bavail > 0,
        "A must reach the per-UID cap while the filesystem still has space",
    )
    return dict(
        allocated=size,
        before=before,
        full=after,
        filesystem_free_bytes=free.f_bavail * free.f_frsize,
    )


def notebook(phase, role, owner):
    path = mount(phase, role) / "workspace" / guest["NOTEBOOK"]
    facts = guest["file_facts"](path)
    require(facts["uid"] == facts["gid"] == owner["uid"], "File UID or GID differs")
    return facts


def write_record(path, data):
    with path.open("xb") as file:
        os.fchmod(file.fileno(), 0o600)
        file.write(json.dumps(data, sort_keys=True).encode())
        file.flush()
        os.fsync(file.fileno())
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_record(path):
    with private_file(path) as file:
        return json.load(file)


@contextmanager
def journal_space(directory, reserve_bytes):
    # A plan, receipt or new directory also consumes the backup disk.
    with disk_admission(directory):
        require_space(available_bytes(directory), 65536 + 8 * os.statvfs(directory).f_frsize, reserve_bytes)
        yield


def start_generation(directory, users, reserve_bytes):
    plan = {}
    for role, owner in users.items():
        source, target = mount("src", role), mount("dst", role)
        before = quota.read(target, owner)
        require(
            before["volume_label"] == "m5quota-dst-" + role and before["hard_bytes"] == 0
            and not os.path.lexists(target / "workspace"),
            "New recovery generation requires fresh, owner-bound destinations",
        )
        quota.check_source_mount(source / "workspace", source)
        policy = quota.snapshot(source, owner)
        require(policy["source_filesystem_uuid"] != before["filesystem_uuid"],
                "Recovery requires separate source and destination volumes")
        plan[role] = dict(quota=policy, inventory=inventory(source / "workspace", owner),
                          notebook=notebook("src", role, owner), empty_target=before)

    with journal_space(directory.parent, reserve_bytes), TemporaryDirectory(dir=directory.parent) as tmp:
        stage = Path(tmp) / "generation"
        stage.mkdir(mode=0o700)
        write_record(stage / "plan.json", plan)
        publish(stage, directory)
    return plan


def owner_package(directory, role, owner, plan, reserve_bytes):
    committed = directory / role
    if not os.path.lexists(committed):
        # Only this locked generation creates the scratch directory. Discard an
        # unpublished partial write after abrupt shutdown, never a committed package.
        partial = directory / (role + ".pending")
        if os.path.lexists(partial):
            private_dir(partial)
            quota.check_source_mount(partial, directory)
            shutil.rmtree(partial)
        with journal_space(directory, reserve_bytes):
            partial.mkdir(mode=0o700)
        try:
            saved = backup(mount("src", role), owner, partial / "package", reserve_bytes=reserve_bytes)
            require(saved["inventory"] == plan["inventory"] and saved["quota"] == plan["quota"],
                    "Offline source changed since the recovery generation was recorded")
            with journal_space(directory, reserve_bytes):
                write_record(partial / "receipt.json", saved)
                publish(partial, committed)
        finally:
            if partial.exists():
                shutil.rmtree(partial)
    private_dir(committed)
    saved = read_record(committed / "receipt.json")
    require(saved["inventory"] == plan["inventory"] and saved["quota"] == plan["quota"],
            "Owner backup receipt differs from the recovery generation")
    return committed / "package", saved


def recover_generation(directory, users, selected_roles, reserve_bytes):
    if not os.path.lexists(directory):
        plan = start_generation(directory, users, reserve_bytes)
    else:
        private_dir(directory)
        plan = read_record(directory / "plan.json")
    require(set(plan) == set(users), "Recovery generation owner set differs")
    # Pin both owners before publishing either target. Earlier complete owner
    # packages survive transient failures on the other owner's backup.
    packages = {}
    for role, owner in users.items():
        identity = {key: owner[key] for key in ("environment", "user_id", "uid", "workspace")}
        require(plan[role]["quota"]["identity"] == identity, "Recovery generation binding changed")
        packages[role] = owner_package(directory, role, owner, plan[role], reserve_bytes)
    result = {}
    for role in selected_roles:
        owner, row = users[role], plan[role]
        target = mount("dst", role)
        current = quota.read(target, owner)
        require(current["filesystem_uuid"] == row["empty_target"]["filesystem_uuid"],
                "Restore destination changed during recovery")
        package, saved = packages[role]
        existing = os.path.lexists(target / "workspace")
        if existing:
            # A prior run may have published the tree before its caller saw the
            # receipt. Never modify that tree. Verify it against the pinned generation.
            quota.check_source_mount(target / "workspace", target)
            private_dir(package)
            with private_file(package / "manifest.json") as stream:
                raw = stream.read(65537)
            require(len(raw) <= 65536 and hashlib.sha256(raw).hexdigest() == saved["manifest_sha256"],
                    "Stored manifest differs from the generation pin")
            manifest = json.loads(raw)
            with private_file(package / "workspace.tar") as stream:
                require(digest(stream) == manifest["archive_sha256"], "Stored archive differs from manifest")
            require(manifest["inventory"] == row["inventory"] and manifest["quota"] == row["quota"],
                    "Stored manifest differs from the generation")
            recovered = dict(inventory=inventory(target / "workspace", owner), quota=current)
        else:
            # An interruption after quota installation but before tree publication
            # leaves this same destination and hard limit safe to retry.
            recovered = restore(target, owner, package, saved["manifest_sha256"])
        facts = notebook("dst", role, owner)
        effective = recovered["quota"]
        require(
            recovered["inventory"] == row["inventory"] and facts == row["notebook"]
            and effective["uid"] == owner["uid"] and effective["accounting"] and effective["enforced"]
            and effective["used_bytes"] <= effective["hard_bytes"] == row["quota"]["hard_bytes"],
            "Quota, ownership or content differs after restore",
        )
        result[role] = dict(empty_target=row["empty_target"], backup=saved, restored=recovered,
                            notebook=facts, already_restored=existing)
    return result


def recover(spec, generation="default", role=None, reserve_bytes=DRILL_BACKUP_FLOOR):
    require(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", generation), "Invalid recovery generation name")
    require(type(reserve_bytes) is int and reserve_bytes > 0, "Expected positive backup reserve")
    base = Path("/backup")
    require(base.is_mount(), "Expected dedicated backup disk")
    directory = base / "web"
    if not directory.exists():
        with journal_space(base, reserve_bytes):
            directory.mkdir(mode=0o700, exist_ok=True)
    private_dir(directory)
    users = spec["users"]
    require(set(users) == {"a", "b"} and (role is None or role in users), "Unexpected recovery owners")
    fd = os.open(directory, os.O_DIRECTORY | os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return recover_generation(directory / ("generation-" + generation), users,
                                  [role] if role else users, reserve_bytes)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "step", choices=("prepare", "inspect", "fill", "unfill", "recover")
    )
    parser.add_argument("--phase", choices=("src", "dst"), default="src")
    parser.add_argument(
        "--generation", default="default",
        help="Named recovery under /backup/web/generation-NAME. Legacy /backup/web/{a,b} packages "
             "remain available through quota-bundle restore with their independently recorded pins.",
    )
    parser.add_argument("--role", choices=("a", "b"), help="Recover or recheck just this owner")
    parser.add_argument("--reserve-bytes", type=int, default=DRILL_BACKUP_FLOOR,
                        help="Minimum available bytes retained on this development drill backup disk")
    args = parser.parse_args()
    users = guest["input_bindings"]()
    a = users["a"]
    if args.step == "prepare":
        result = {}
        for role, owner in users.items():
            source = mount("src", role) / "workspace"
            initial = inventory(source, owner)
            require(
                initial == owner["inventory"],
                "Source workspace differs from the sealed archive",
            )
            result[role] = dict(
                inventory=initial, notebook=notebook("src", role, owner)
            )
        result["release"] = guest["run_as"](
            a, release_candidate, mount("src", "a") / "workspace" / RELEASE
        )
    elif args.step == "inspect":
        result = {}
        for role, owner in users.items():
            disk = mount(args.phase, role)
            result[role] = dict(
                notebook=notebook(args.phase, role, owner),
                kernel=quota.read(disk, owner),
            )
    elif args.step == "fill":
        result = fill(a)
    elif args.step == "unfill":
        source = mount("src", "a") / "workspace"
        require(
            not (source / RELEASE).exists(), "Browser has not removed the release file"
        )
        before = quota.read(source.parent, a)
        guest["run_as"](a, guest["release"], source)
        result = dict(
            after_web_retry=before, after_fixture_cleanup=quota.read(source.parent, a)
        )
    else:
        result = recover({"users": users}, args.generation, args.role, args.reserve_bytes)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

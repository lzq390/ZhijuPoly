"""Run the dev-m1 hard-quota drill inside the disposable, offline M5 VM only."""

import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile

import openscience_m5_archive as archive
import openscience_m5_quota_bundle as bundle
import quota

INPUTS = Path("/inputs")
ROLES = ("a", "b")
MOUNT_ROOT = Path("/mnt/openscience-m5-quota")
BACKUP = Path("/backup")
NOTEBOOK = Path("files/projects/same-project/notebook.txt")
PROBE = Path("files/projects/same-project/quota-write-test.bin")
FILLER = Path("files/general/.quota-drill-reservation")
LIMIT = 2_000_000_000
MIB = 1024 * 1024


def check(ok, detail):
    if not ok:
        raise ValueError(detail)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise RuntimeError(
            f"{' '.join(args)} failed ({result.returncode}): {detail[:350]}"
        )


def quota_kernel():
    release = os.uname().release
    config = Path("/boot") / ("config-" + release)
    relevant = ("CONFIG_QUOTA=", "CONFIG_QUOTA_TREE=", "CONFIG_QFMT_V2=")
    settings = (
        [line for line in config.read_text().splitlines() if line.startswith(relevant)]
        if config.is_file()
        else []
    )
    modules = [
        name
        for name in ("quota_tree", "quota_v2")
        if (Path("/sys/module") / name).is_dir()
    ]
    return {
        "release": release,
        "quota_config": settings,
        "resident_quota_modules": modules,
    }


def input_bindings():
    spec = json.loads((INPUTS / "spec.json").read_bytes())
    check(
        spec["version"] == 1
        and spec["environment"] == "dev-m1"
        and spec["hard_limit_bytes"] == LIMIT
        and set(spec["users"]) == set(ROLES),
        "Wrong drill specification",
    )
    users = spec["users"]
    for role in ROLES:
        row = users[role]
        check(
            row["environment"] == "dev-m1"
            and row["user_id"] == "test-user-" + role
            and row["uid"] == row["gid"]
            and row["uid"] > 1000,
            "Invalid dev workspace identity",
        )
        package = INPUTS / role
        check(
            sha256(package / "manifest.json") == row["manifest_sha256"],
            "Unrecognized source manifest",
        )
        manifest = json.loads((package / "manifest.json").read_bytes())
        check(
            all(
                row[k] == manifest[k]
                for k in (
                    "uid",
                    "user_id",
                    "workspace",
                    "environment",
                    "inventory",
                    "archive_sha256",
                )
            ),
            "Source archive ownership or inventory differs",
        )
        check(
            sha256(package / "workspace.tar") == row["archive_sha256"],
            "Source tar checksum mismatch",
        )
    check(
        users["a"]["workspace"] != users["b"]["workspace"]
        and users["a"]["uid"] != users["b"]["uid"],
        "Source workspaces must be separate",
    )
    return users


def blank_volumes():
    # A dedicated VM creates four brand-new whole disks using these serials. Validate the entire set first.
    disks = {}
    for phase, serial_prefix in (("src", "m5-"), ("dst", "m5-restore-")):
        for role in ROLES:
            serial = serial_prefix + role
            device = Path("/dev/disk/by-id/virtio-" + serial).resolve(strict=True)
            sysblock = Path("/sys/class/block") / device.name
            check(
                stat.S_ISBLK(device.stat().st_mode)
                and not (sysblock / "partition").exists()
                and (sysblock / "serial").read_text().strip() == serial,
                "Unexpected scratch disk",
            )
            size = int((sysblock / "size").read_text().strip()) * 512
            check(
                size >= 3 * 1024**3 and not any((sysblock / "holders").iterdir()),
                "Scratch disk not available",
            )
            dev = device.stat().st_rdev
            check(
                dev not in {p.stat().st_rdev for p in disks.values()},
                "Aliased scratch disks",
            )
            mounts = Path("/proc/self/mountinfo").read_text().splitlines()
            check(
                all(
                    line.split()[2] != f"{os.major(dev)}:{os.minor(dev)}"
                    for line in mounts
                ),
                "Scratch disk mounted",
            )
            probe = subprocess.run(["blkid", "-p", str(device)], capture_output=True)
            check(
                probe.returncode == 2 and not probe.stdout.strip(),
                "Scratch disk contains a filesystem or partition table",
            )
            with device.open("rb", buffering=0) as stream:
                check(stream.read(4096) == bytes(4096), "Expected blank disk header")
                stream.seek(size - 4096)
                check(stream.read(4096) == bytes(4096), "Expected blank disk trailer")
            disks[(phase, role)] = device
    return disks


def mount_volumes(disks, mounted):
    volumes = {}
    MOUNT_ROOT.mkdir(exist_ok=False)
    MOUNT_ROOT.chmod(0o755)
    for (phase, role), device in disks.items():
        target = MOUNT_ROOT / (phase + "-" + role)
        target.mkdir()
        label = f"m5quota-{phase}-{role}"
        command(
            "mkfs.ext4",
            "-q",
            "-F",
            "-O",
            "quota",
            "-b",
            "4096",
            "-m",
            "0",
            "-L",
            label,
            str(device),
        )
        command(
            "mount",
            "-t",
            "ext4",
            "-o",
            "usrquota,nodev,nosuid,noexec",
            str(device),
            str(target),
        )
        mounted.append(target)
        check(
            target.stat().st_dev == device.stat().st_rdev,
            "Unexpected mounted filesystem",
        )
        volumes[(phase, role)] = target
    return volumes


def run_as(binding, fn, *args):
    # The coordinator can configure quotas. Each file I/O check runs under the actual unprivileged UID.
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        with os.fdopen(write_fd, "w") as out:
            try:
                os.setgroups([])
                os.setresgid(*([binding["gid"]] * 3))
                os.setresuid(*([binding["uid"]] * 3))
                os.umask(0o077)
                check(
                    all(v == binding["uid"] for v in os.getresuid())
                    and not os.getgroups(),
                    "UID drop failed",
                )
                caps = next(
                    v
                    for v in Path("/proc/self/status").read_text().splitlines()
                    if v.startswith("CapEff:")
                )
                check(int(caps.split()[1], 16) == 0, "Test write retained a capability")
                result = {"result": fn(*args)}
            except BaseException as e:
                result = {"error": str(e), "kind": type(e).__name__}
            json.dump(result, out)
        os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd) as stream:
        raw = stream.read()
    _, status = os.waitpid(pid, 0)
    check(
        os.waitstatus_to_exitcode(status) == 0 and raw,
        "Unprivileged worker exited before reporting",
    )
    outcome = json.loads(raw)
    if "error" in outcome:
        raise RuntimeError(
            f"{binding['user_id']} {fn.__name__}: {outcome['kind']}: {outcome['error']}"
        )
    return outcome["result"]


def file_facts(path):
    item = path.stat()
    return {
        "sha256": sha256(path),
        "uid": item.st_uid,
        "gid": item.st_gid,
        "mode": stat.S_IMODE(item.st_mode),
    }


def private_read_denied(path):
    try:
        with path.open("rb") as f:
            f.read(1)
    except PermissionError as e:
        check(e.errno == errno.EACCES, "Foreign read returned unexpected errno")
        return e.errno
    raise ValueError("Foreign view could read a private file")


def read_workspace(workspace, row):
    uid = row["uid"]
    st = workspace.stat()
    check(
        (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (uid, uid, 0o700),
        "Workspace ownership or mode differs",
    )
    check(
        (workspace / ".m1-owner").read_text() == row["workspace"],
        "Workspace marker differs",
    )
    info = file_facts(workspace / NOTEBOOK)
    check(
        info["sha256"] == row["notebook_sha256"] and info["uid"] == info["gid"] == uid,
        "Original notebook content or owner differs",
    )
    return info


def reserve_to_limit(workspace, hard_bytes, charged_before):
    block = os.statvfs(workspace).f_frsize
    check(block == 4096, "Expected 4 KiB filesystem blocks")
    offset = 0
    with (workspace / FILLER).open("xb", buffering=0) as out:
        # Reserve real extents without copying gigabytes of zeroes into the host. Finish to one block.
        first = max(block, ((hard_bytes - charged_before - 4 * MIB) // block) * block)
        for chunk in (first, MIB, 64 * 1024, block):
            while offset + chunk <= hard_bytes + 2 * block:
                try:
                    os.posix_fallocate(out.fileno(), offset, chunk)
                except OSError as e:
                    if e.errno != errno.EDQUOT:
                        raise
                    break
                offset += chunk
        os.fsync(out.fileno())
        allocated = os.fstat(out.fileno()).st_blocks * 512
    check(
        allocated >= hard_bytes * 0.9 and allocated >= offset,
        "Insufficient real extents charged to owner",
    )
    return allocated


def normal_write(path, content, expect_full):
    stage = "open"
    try:
        with path.open("wb", buffering=0) as out:
            stage = "write"
            written = out.write(content)
            check(written == len(content), "Short write")
            stage = "fsync"
            os.fsync(out.fileno())
    except OSError as e:
        if not expect_full or e.errno != errno.EDQUOT:
            raise
        return {"errno": e.errno, "error": "EDQUOT", "stage": stage}
    check(not expect_full, "Ordinary write unexpectedly succeeded at hard limit")
    check(path.read_bytes() == content, "Read-back after write differs")
    return file_facts(path)


def release(workspace):
    (workspace / FILLER).unlink()


def payload(role):
    return ((f"dev-m1 hard quota {role}\n".encode()) * 512)[:4096]


def begin_full(volume, row):
    workspace = volume / "workspace"
    before = quota.read(volume, row)
    charged = run_as(
        row, reserve_to_limit, workspace, before["hard_bytes"], before["used_bytes"]
    )
    # A new filename, in addition to a write against a regular fd. Keep the recovery marker intact.
    path = workspace / "files/general/quota-full-new.bin"
    check(not path.exists(), "Full-boundary probe already exists")
    denied = run_as(row, normal_write, path, bytes(4096), True)
    check(
        denied["stage"] in {"write", "fsync"},
        "Limit must reject ordinary file data I/O",
    )
    after = quota.read(volume, row)
    free = os.statvfs(volume)
    free_bytes = free.f_bavail * free.f_frsize
    check(
        free_bytes > 256 * MIB and LIMIT - free.f_frsize < after["used_bytes"] <= LIMIT,
        "Limit was not reached with sufficient backing disk free",
    )
    return {
        "before": before,
        "at_limit": after,
        "backing_free_bytes": free_bytes,
        "allocated_bytes": charged,
        "new_file_write": denied,
        "notebook": run_as(row, read_workspace, workspace, row),
    }


def finish_full(volume, row, role):
    workspace = volume / "workspace"
    run_as(row, release, workspace)
    path = workspace / "files/general/quota-full-new.bin"
    # Retry the exact path and buffer previously rejected by the filesystem.
    retried = run_as(row, normal_write, path, bytes(4096), False)
    run_as(row, Path.unlink, path)
    result = run_as(row, normal_write, workspace / PROBE, payload(role), False)
    after = quota.read(volume, row)
    check(after["used_bytes"] < LIMIT // 2, "Deleting the filler did not release space")
    return {
        "retried_original_write": retried,
        "new_file": result,
        "after_release": after,
    }


def inventory(workspace, row):
    with tempfile.TemporaryFile() as stream:
        with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            archive.add_tree(workspace, tar, row["uid"], row["workspace"])
        stream.seek(0)
        return archive.process_archive(stream, row["uid"], row["workspace"])


def trial():
    check(
        os.geteuid() == 0 and os.path.ismount(INPUTS) and os.path.ismount(BACKUP),
        "Expected isolated VM mounts",
    )
    check(
        os.statvfs(INPUTS).f_flag & os.ST_RDONLY
        and INPUTS.stat().st_dev != BACKUP.stat().st_dev
        and BACKUP.stat().st_dev != Path("/").stat().st_dev,
        "Input and backup disks must be separate",
    )
    users = input_bindings()
    disks = blank_volumes()
    mounted = []
    try:
        volumes = mount_volumes(disks, mounted)
        check(
            BACKUP.stat().st_dev not in {p.stat().st_dev for p in volumes.values()},
            "Backup on workspace disk",
        )
        result = {
            "environment": "dev-m1",
            "policy_bytes": LIMIT,
            "kernel": quota_kernel(),
            "users": {},
        }
        packages = {}
        for role, row in users.items():
            volume = volumes[("src", role)]
            kernel = quota.install(volume, row, LIMIT)
            check(
                kernel["hard_bytes"] == LIMIT and kernel["enforced"],
                "Kernel hard limit not enforced",
            )
            workspace = volume / "workspace"
            workspace.mkdir()
            with (INPUTS / role / "workspace.tar").open("rb") as source:
                incoming = archive.process_archive(
                    source, row["uid"], row["workspace"], workspace
                )
            check(
                incoming == row["inventory"] == inventory(workspace, row),
                "Initial dev-m1 content differs",
            )
            result["users"][role] = {
                "user_id": row["user_id"],
                "workspace": row["workspace"],
                "uid": row["uid"],
                "gid": row["gid"],
                "source_kernel": kernel,
                "initial_inventory": incoming,
                "original_notebook": run_as(row, read_workspace, workspace, row),
            }
        a, b = (volumes[("src", key)] for key in ROLES)
        result["source_a_full"] = begin_full(a, users["a"])
        result["b_write_while_a_full"] = run_as(
            users["b"], normal_write, b / "workspace" / PROBE, payload("b"), False
        )
        result["a_still_blocked_after_b_write"] = run_as(
            users["a"], normal_write, a / "workspace" / PROBE, payload("a"), True
        )
        result["source_a_recovered"] = finish_full(a, users["a"], "a")
        for role in ROLES:
            row = users[role]
            workspace = volumes[("src", role)] / "workspace"
            receipt = bundle.backup(workspace.parent, row, BACKUP / role)
            expected = inventory(workspace, row)
            check(receipt["inventory"] == expected, "Backup archive inventory differs")
            packages[role] = receipt["manifest_sha256"]
            other = "b" if role == "a" else "a"
            result["users"][role].update(
                backup_manifest_sha256=packages[role],
                backup_inventory=expected,
                source_cross_read_errno=run_as(
                    row,
                    private_read_denied,
                    volumes[("src", other)] / "workspace" / NOTEBOOK,
                ),
            )
        for role in ROLES:
            row = users[role]
            dest = volumes[("dst", role)]
            other = "b" if role == "a" else "a"
            check(
                quota.read(dest, row)["hard_bytes"] == 0,
                "Fresh target already has an owner limit",
            )
            try:
                bundle.restore(dest, row, BACKUP / other, packages[other])
            except (ValueError, RuntimeError):
                check(
                    not (dest / "workspace").exists()
                    and quota.read(dest, row)["hard_bytes"] == 0,
                    "Wrong-owner archive mutated target",
                )
            else:
                raise ValueError("Wrong-owner archive was accepted")
            restored = bundle.restore(dest, row, BACKUP / role, packages[role])
            # Fresh mount verifies persistent contents and quota state before any new owner writes.
            command("umount", str(dest))
            command(
                "mount",
                "-t",
                "ext4",
                "-o",
                "usrquota,nodev,nosuid,noexec",
                str(disks[("dst", role)]),
                str(dest),
            )
            workspace = dest / "workspace"
            kernel = quota.read(dest, row)
            check(
                kernel["enforced"] and kernel["hard_bytes"] == LIMIT,
                "Restored quota lost on remount",
            )
            check(
                kernel["filesystem_uuid"]
                != result["users"][role]["source_kernel"]["filesystem_uuid"],
                "Recovery reused the source filesystem",
            )
            checked_tree = inventory(workspace, row)
            check(
                restored["inventory"]
                == checked_tree
                == result["users"][role]["backup_inventory"],
                "Restored tree differs from backup",
            )
            notebook = run_as(row, read_workspace, workspace, row)
            new_file = run_as(row, file_facts, workspace / PROBE)
            check(
                notebook == result["users"][role]["original_notebook"]
                and new_file["sha256"] == hashlib.sha256(payload(role)).hexdigest()
                and new_file["uid"] == new_file["gid"] == row["uid"],
                "Recovered user files differ",
            )
            result["users"][role].update(
                restored_kernel=kernel,
                restored_inventory=checked_tree,
                restored_notebook=notebook,
                restored_new_file=new_file,
                wrong_owner_refused_before_writes=True,
            )
        for role in ROLES:
            row = users[role]
            dest = volumes[("dst", role)]
            other = "b" if role == "a" else "a"
            result["users"][role].update(
                restored_cross_read_errno=run_as(
                    row,
                    private_read_denied,
                    volumes[("dst", other)] / "workspace" / NOTEBOOK,
                ),
                restored_full=begin_full(dest, row),
                restored_delete_and_retry=finish_full(dest, row, role),
            )
        return {"status": "passed", **result}
    finally:
        for target in reversed(mounted):
            if os.path.ismount(target):
                command("umount", str(target))


if __name__ == "__main__":
    try:
        observed = trial()
    except Exception as failure:
        sys.exit(
            f"{type(failure).__name__}: {failure}; kernel={json.dumps(quota_kernel())}"
        )
    print("M5_QUOTA_RESULT=" + json.dumps(observed, separators=(",", ":")), flush=True)

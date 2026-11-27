"""Storage-side checks for the disposable two-guest development restore rehearsal."""

import errno
import json
import os
from pathlib import Path
import secrets
import subprocess
import tarfile
from time import monotonic

import openscience_m5_archive as codec
import openscience_m5_quota_bundle as bundle
import quota
import quota_guest as guest


PROJECT = Path("files/projects/same-project")
ATTACHMENT = PROJECT / "attachment.bin"
NOTEBOOK = PROJECT / "notebook.txt"
RESERVED = PROJECT / "reserved.bin"
CHAT = Path("records/projects/same-project/conversation.json")
BACKUP = Path("/backup")


def device(serial, blank):
    found = [p for p in Path("/sys/class/block").glob("vd*") if (p / "serial").is_file()
             and (p / "serial").read_text().strip() == serial]
    guest.check(len(found) == 1, "Expected a unique rehearsal device " + serial)
    path = Path("/dev") / found[0].name
    if blank:
        probe = subprocess.run(["blkid", "-p", str(path)], capture_output=True)
        guest.check(probe.returncode == 2 and not probe.stdout.strip(), "Expected a blank rehearsal disk")
    return path


def create_fixture(ws, owner):
    for name in sorted(codec.SKELETON - {"."}, key=lambda n: (len(n), n)):
        (ws / name).mkdir(mode=0o700, exist_ok=True)
    (ws / PROJECT).mkdir()
    (ws / CHAT).parent.mkdir(parents=True, exist_ok=True)
    (ws / NOTEBOOK).write_text(secrets.token_hex(32))
    (ws / ATTACHMENT).write_bytes(os.urandom(65536))
    (ws / CHAT).write_text(json.dumps({"user": owner["user_id"], "text": secrets.token_hex(24),
                                       "attachment": str(ATTACHMENT)}))
    (ws / RESERVED).touch()
    guest.command("fallocate", "--keep-size", "--offset", "65536", "--length", "262144", str(ws / RESERVED))


def facts(ws):
    result = {str(path): guest.file_facts(ws / path) for path in (NOTEBOOK, CHAT, ATTACHMENT, RESERVED)}
    with (ws / RESERVED).open("rb") as f:
        length = os.fstat(f.fileno()).st_size
        layout = codec.fiemap_layout(f.fileno(), length)
        guest.check(length == 0 and layout and layout[1] == [(65536, 262144)], "Unwritten reservation changed")
        result[str(RESERVED)]["reserved_ranges"] = layout[1]
    return result


def check_writes(ws):
    path = ws / PROJECT / "resumed.txt"
    with path.open("xb") as out:
        try:
            os.posix_fallocate(out.fileno(), 0, guest.LIMIT + 4096)
        except OSError as error:
            if error.errno != errno.EDQUOT:
                raise
        else:
            raise ValueError("Restored UID exceeded the hard limit")
    path.unlink()
    payload = os.urandom(16)
    path.write_bytes(payload)
    guest.check(path.read_bytes() == payload, "Owner write/read failed")
    return {"over_limit_errno": errno.EDQUOT, "new_write_bytes": len(payload)}


def private_write_denied(path):
    try:
        fd = os.open(path, os.O_WRONLY)
    except PermissionError as error:
        guest.check(error.errno == errno.EACCES, "Foreign write returned unexpected errno")
        return error.errno
    os.close(fd)
    raise ValueError("Foreign user could open a private file for writing")


def run():
    spec = json.loads(Path("/inputs/run.json").read_bytes())
    phase, users = spec["phase"], spec["users"]
    guest.check(phase in ("src", "dst") and len(users) == 2, "Invalid rehearsal phase")
    guest.check(Path("/sys/class/dmi/id/product_uuid").read_text().strip().lower() == spec["machine_uuid"],
                "Use only the intended disposable guest")
    disks = {(phase, role): device(f"m6-{phase}-{role}", True) for role in users}
    serials = sorted(path.read_text().strip() for path in Path("/sys/class/block").glob("vd*/serial"))
    guest.check(set(filter(None, serials)) == {"m6-system", "m6-input", "m6-seed", "m6-transfer",
                                                *("m6-" + phase + "-" + role for role in users)},
                "Unexpected guest disk configuration")
    transfer = device("m6-transfer", phase == "src")
    mounted = []
    try:
        volumes = guest.mount_volumes(disks, mounted)
        BACKUP.mkdir()
        if phase == "src":
            guest.command("mkfs.ext4", "-q", "-m", "0", "-L", "m6-transfer", str(transfer))
        options = "nodev,nosuid,noexec" if phase == "src" else "ro,noload,nodev,nosuid,noexec"
        guest.command("mount", "-t", "ext4", "-o", options, str(transfer), str(BACKUP))
        mounted.append(BACKUP)
        if phase == "src":
            BACKUP.chmod(0o700)
        else:
            guest.check(bool(os.statvfs(BACKUP).f_flag & os.ST_RDONLY), "Backup medium must be read-only")
        results = {}
        for role, owner in users.items():
            mount = volumes[phase, role]
            ws, package = mount / "workspace", BACKUP / role
            if phase == "src":
                policy = quota.install(mount, owner)
                ws.mkdir(mode=0o700)
                if spec.get("native_pins"):
                    archive = Path("/inputs") / (role + ".tar")
                    guest.check(guest.sha256(archive) == spec["native_pins"][role], "Wrong source fixture checksum")
                    with archive.open("rb") as src:
                        codec.process_archive(src, owner["uid"], owner["workspace"], ws)
                else:
                    os.chown(ws, owner["uid"], owner["gid"])
                    (ws / ".m1-owner").write_text(owner["workspace"])
                    (ws / ".m1-owner").chmod(0o444)
                guest.run_as(owner, create_fixture, ws, owner)
                snapshot = bundle.backup(mount, owner, package, reserve_bytes=8 * guest.MIB)
                results[role] = dict(manifest=snapshot["manifest_sha256"], inventory=snapshot["inventory"])
            else:
                opposite = "b" if role == "a" else "a"
                try:
                    bundle.restore(mount, owner, BACKUP / opposite, spec["pins"][opposite])
                except ValueError as error:
                    guest.check("does not belong to the target binding" in str(error), str(error))
                else:
                    raise ValueError("Foreign backup was accepted")
                guest.check(not ws.exists() and quota.read(mount, owner)["hard_bytes"] == 0,
                            "Wrong-owner attempt changed the empty destination")
                started = monotonic()
                recovered = bundle.restore(mount, owner, package, spec["pins"][role])
                policy = recovered["quota"]
                results[role] = dict(inventory=recovered["inventory"], foreign_package_refused=True,
                                     restore_seconds=round(monotonic() - started, 3))
                if role == "a":
                    b_mount = volumes[phase, "b"]
                    guest.check(not (b_mount / "workspace").exists() and quota.read(b_mount, users["b"])["hard_bytes"] == 0,
                                "Restoring A changed B")
            row = results[role]
            row.update(facts=guest.run_as(owner, facts, ws), uid=policy["uid"], hard_bytes=policy["hard_bytes"],
                       fs_uuid=policy["filesystem_uuid"], enforced=policy["enforced"], accounting=policy["accounting"])
            guest.check(row["uid"] == owner["uid"] and row["hard_bytes"] == guest.LIMIT
                        and row["enforced"] and row["accounting"], "Quota mismatch")
            if phase == "dst":
                if spec.get("export"):
                    export = mount / "m6-export.tar"
                    with export.open("xb") as out:
                        with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as writer:
                            codec.add_tree(ws, writer, owner["uid"], owner["workspace"])
                        out.flush()
                        os.fsync(out.fileno())
                    row["export_sha256"] = guest.sha256(export)
                row.update(guest.run_as(owner, check_writes, ws))
        for role, other in (("a", "b"), ("b", "a")):
            foreign_file = volumes[phase, other] / "workspace" / ATTACHMENT
            results[role]["foreign_read_errno"] = guest.run_as(users[role], guest.private_read_denied, foreign_file)
            results[role]["foreign_write_errno"] = guest.run_as(users[role], private_write_denied, foreign_file)
        return {"boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "serials": serials, "accounts": results}
    finally:
        for mount in reversed(mounted):
            guest.command("umount", str(mount))


if __name__ == "__main__":
    print("M6_RESTORE_RESULT=" + json.dumps(run(), sort_keys=True), flush=True)

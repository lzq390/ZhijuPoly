"""Attest the dev-m1 ext4 quota server and its dedicated NFSv4.1 Docker volume."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile
from uuid import UUID

BACKEND = "nfs-ext4-user-quota"
SLOTS = ("src-a", "src-b", "dst-a", "dst-b")
GUEST_ROOT = "/mnt/openscience-m5-quota"
GUEST_SCRIPT = "/opt/nexpoly/ops/openscience-per-user-dev/quota_nfs.py"
IDENTITY = ("environment", "user_id", "workspace", "uid", "mount", "filesystem_uuid")
CLAIMS = Path("/opt/nexpoly-m5-nfs/claims")


def private_content(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("Expected a canonical absolute operator file")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
            raise ValueError("Expected a private operator file with mode 0600")
        return source.read(65537)


def endpoints(manifest, phase, bindings):
    obj = json.loads(private_content(manifest))
    if (obj.get("format") != "nexpoly-m5-nfs-v1" or phase not in ("src", "dst")
            or obj.get("ssh_alias") != "m5-quota-guest" or not re.fullmatch("[0-9a-f]{12}", obj["run_id"])
            or type(obj["port"]) is not int or not 1024 <= obj["port"] <= 65535
            or obj["port"] == 9011 or set(obj["workspaces"]) != set(SLOTS)):
        raise ValueError("Expected the private four-volume M5 NFS pilot manifest")
    config_hash = hashlib.sha256(private_content(obj["ssh_config"])).hexdigest()
    result = {}
    for b in bindings:
        slot = Path(b["mount"]).name
        if (slot not in {phase + "-a", phase + "-b"}
                or (b["environment"] == "dev-m1" and b["user_id"] != "test-user-" + slot[-1])):
            raise ValueError("Volume slot and registered quota owner differ")
        row = obj["workspaces"][slot]
        vol = "nexpoly-m5-" + obj["run_id"] + "-" + slot
        if (row.get("volume") != vol or row.get("pseudo") != "/m5-" + slot
                or b["mount"] != GUEST_ROOT + "/" + slot or any(row.get(k) != b[k] for k in IDENTITY)):
            raise ValueError("NFS volume manifest and registered quota binding differ")
        result[b["environment"] + "/" + b["user_id"]] = {
            "run_id": obj["run_id"], "slot": slot, "volume": vol, "port": obj["port"],
            "ssh_config": obj["ssh_config"], "ssh_sha256": config_hash,
        }
    return result


def inspect_volume(endpoint, binding, docker):
    from openscience_m5_native_storage import mount_options, volume_labels

    slot = endpoint["slot"]
    fmt = "{{json .Driver}}\n{{json .Options}}\n{{json .Labels}}"
    observed = docker("volume", "inspect", "--format", fmt, endpoint["volume"])
    driver, options, labels = map(json.loads, observed.splitlines())
    row = {"pseudo": "/m5-" + slot, "filesystem_uuid": binding["filesystem_uuid"]}
    expected = mount_options(endpoint, row)
    owners = volume_labels(endpoint, slot, row)
    if driver != "local" or options != expected or any((labels or {}).get(k) != v for k, v in owners.items()):
        raise ValueError("Docker NFS volume driver, options or ownership differ from the registered endpoint")


def candidate_medium(endpoint):
    from openscience_m5_backup import physical_disk

    vm = Path(endpoint["ssh_config"]).parent
    config = json.loads(private_content(vm / "vm.json"))
    phase, role = endpoint["slot"].split("-")
    if phase == "src":
        root, device, serial = vm, "primary", "m5-" + role
    elif config.get("restore_target") is True:
        root, device, serial = vm, "primary", "m5-restore-" + role
    else:
        root, device, serial = Path(config["backup_dir"]), "backup", "m5-restore-" + role
    if config["format"] != "nexpoly-m5-isolated-browser-v1" or root != root.resolve(strict=True):
        raise ValueError("Expected the registered storage guest and its canonical host directory")
    backing = root / (serial + ".raw")
    dev = backing.lstat().st_dev
    filesystem = f"{os.major(dev)}:{os.minor(dev)}"
    disk, _ = physical_disk(filesystem)
    if disk != config["host_disks"][device]:
        raise ValueError("Deployment storage is on another physical disk")
    pin = {"file": str(backing), "device": filesystem, "inode": config["disk_inodes"][serial]}
    host_free_mib(pin)
    return pin


def host_space(pin):
    # Measure on the open backing image, even when Docker and the quota guest live on different disks.
    fd = os.open(pin["file"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        device = f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or (device, info.st_ino) != (pin["device"], pin["inode"])):
            raise ValueError("Deployment storage medium differs from its registered host image")
        space = os.fstatvfs(fd)
        return space.f_bavail * space.f_frsize, space.f_blocks * space.f_frsize
    finally:
        os.close(fd)


def host_free_mib(pin):
    available, _ = host_space(pin)
    return available // 1024**2


def guest_client(endpoint):
    from openscience_m5_storage_ssh import StorageGuest

    if hashlib.sha256(private_content(endpoint["ssh_config"])).hexdigest() != endpoint["ssh_sha256"]:
        raise ValueError("NFS quota attestation SSH config differs from the registered pin")
    return StorageGuest(endpoint["ssh_config"])


def ownership(registry):
    return (registry.data["deployment"], hashlib.sha256(os.fsencode(registry.root.resolve())).hexdigest())


def claim_record(phase, deployment, root_hash, a_uuid, b_uuid):
    if (phase not in ("src", "dst") or not re.fullmatch("[a-f0-9]{32}", deployment)
            or not re.fullmatch("[a-f0-9]{64}", root_hash) or a_uuid == b_uuid
            or any(str(UUID(value)) != value for value in (a_uuid, b_uuid))):
        raise ValueError("Invalid M5 native registry claim")
    return {"phase": phase, "deployment": deployment, "registry_sha256": root_hash,
            "filesystem_uuids": {"a": a_uuid, "b": b_uuid}}


def apply_claim(directory, record, acquire):
    if acquire:
        directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Expected a private operator-owned claim directory")
    dest = directory / (record["phase"] + ".json")
    if acquire and not dest.exists():
        # Publish a fully fsynced file without replacing any racing claimant.
        with NamedTemporaryFile(dir=directory) as tmp:
            tmp.write(json.dumps(record, sort_keys=True).encode())
            tmp.flush()
            os.fsync(tmp.fileno())
            try:
                os.link(tmp.name, dest)
            except FileExistsError:
                pass
    if json.loads(private_content(dest)) != record:
        raise ValueError("Quota filesystems are claimed by another registry")
    if acquire:
        # The first claim also creates the directory itself. Persist both directory entries;
        # a concurrent replay can observe the new link before its creator has synced it.
        for path in (directory, directory.parent):
            fd = os.open(path, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    return record


def pinned_guest_claim(phase, deployment, root_hash, *, acquire, supplied_uuids=None):
    disk_pins = json.loads(Path("/etc/nexpoly-m5-disks.json").read_bytes())
    prefix = "m5-" if phase == "src" else "m5-restore-"
    uuids = [disk_pins[prefix + r] for r in "ab"]
    if supplied_uuids is not None and list(supplied_uuids) != uuids:
        raise ValueError("Claim request differs from the durable quota disk UUIDs")
    return apply_claim(CLAIMS, claim_record(phase, deployment, root_hash, *uuids), acquire)


def claim_pair(registry, endpoints, bindings, docker):
    held = False
    for key, ep in endpoints.items():
        marker = "label=org.nexpoly.m5.filesystem-uuid=" + bindings[key]["filesystem_uuid"]
        aliases = set(docker("volume", "ls", "-q", "--filter", marker).splitlines()) - {ep["volume"]}
        for alias in aliases:
            if docker("ps", "-aq", "--no-trunc", "--filter", "volume=" + alias):
                raise ValueError("Another container holds an alias for the requested NFS volume")
        clients = set(docker("ps", "-aq", "--no-trunc", "--filter", "volume=" + ep["volume"]).splitlines())
        if clients:
            held = True
            own = registry.workspace(*key.split("/", 1)).resource("backend") if key in registry.data["workspaces"] else None
            if not own or clients != {own["id"]}:
                raise ValueError("Another container holds the requested NFS volume")
    ordered = sorted(endpoints, key=lambda key: endpoints[key]["slot"])
    first = endpoints[ordered[0]]
    phase = first["slot"][:3]
    if [endpoints[key]["slot"] for key in ordered] != [phase + "-a", phase + "-b"]:
        raise ValueError("Expected the two distinct quota slots belonging to one phase")
    uuids = [bindings[key]["filesystem_uuid"] for key in ordered]
    owner = ownership(registry)
    record = claim_record(phase, *owner, *uuids)
    # An existing owner can replay registration while its own backend runs. Creating or
    # adopting a new claim requires both volumes to be unused.
    action = "check-claim" if held else "claim"
    try:
        result = guest_client(first).run("python3", GUEST_SCRIPT, action, phase, *owner, *uuids,
                                        capture_output=True, timeout=25)
    except subprocess.CalledProcessError as error:
        if b"Quota filesystems are claimed by another registry" in (error.stderr or b""):
            raise ValueError("Quota filesystems are claimed by another registry") from error
        raise ValueError("Storage guest refused the exclusive native registry claim") from error
    if json.loads(result.stdout) != record:
        raise ValueError("Storage guest returned an unexpected registry claim")
    return record


def attest(endpoint, binding, docker, *, owner=None):
    inspect_volume(endpoint, binding, docker)
    slot = endpoint["slot"]
    guest = guest_client(endpoint)
    command = ("attest-owned", slot, owner["deployment"], owner["registry_sha256"]) if owner else ("attest", slot)
    raw = guest.run("python3", GUEST_SCRIPT, *command, capture_output=True, timeout=25).stdout
    result = json.loads(raw)
    if owner and result.get("claim") != owner:
        raise ValueError("Quota attestation belongs to a different native registry")
    obs = result["quota"]
    expected_label = "m5quota-" + slot
    if binding["environment"] == "deploy-m6":
        from openscience_per_user_dev import pilot_module
        expected_label = pilot_module("quota").volume_label(binding, slot[:3])
    if (result.get("binding") != {k: binding[k] for k in IDENTITY} or obs.get("filesystem_type") != "ext4"
            or obs.get("volume_label") != expected_label or obs.get("filesystem_uuid") != binding["filesystem_uuid"]
            or obs.get("uid") != binding["uid"] or obs.get("hard_bytes") != 2_000_000_000
            or obs.get("accounting") is not True or obs.get("enforced") is not True):
        raise ValueError("Remote quota attestation and the registered ext4 identity differ")
    return obs


# Executed under the workspace UID in a read-only preflight helper, and again in the real backend.
# mountinfo is private to that process's mount namespace. Reject nested mounts that could bypass quota.
CLIENT_CHECK = """
import os, stat, sys
from pathlib import Path
root, slot, port, workspace, wanted = sys.argv[1:]
records = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
matches = [line for line in records if line[4] == root]
if len(matches) != 1 or any(line[4].startswith(root + '/') for line in records):
    raise SystemExit('Expected one NFS workspace mount with no nested mounts')
line = matches[0]
fstype, source, options = line[line.index('-') + 1:]
required = {'vers=4.1', 'hard', 'proto=tcp', 'port=' + port, 'addr=127.0.0.1', 'sec=sys'}
if fstype != 'nfs4' or source != ':/m5-' + slot + '/workspace' or not required <= set(options.split(',')):
    raise SystemExit('Unexpected live NFS endpoint or protocol')
uid = int(wanted)
info = os.stat(root)
if (os.getuid(), os.getgid(), info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (uid, uid, uid, uid, 0o700):
    raise SystemExit('Unexpected live NFS workspace UID or mode')
fd = os.open(Path(root) / '.m1-owner', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
with os.fdopen(fd, 'rb') as marker:
    meta = os.fstat(fd)
    if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or meta.st_gid != 0 or stat.S_IMODE(meta.st_mode) != 0o444
            or meta.st_dev != info.st_dev or marker.read(33) != workspace.encode('ascii')):
        raise SystemExit('Unexpected live NFS workspace marker')
"""


def client_args(endpoint, binding, mount):
    return (mount, endpoint["slot"], str(endpoint["port"]), binding["workspace"], str(binding["uid"]))


def guest_attestation(slot):
    # The caller supplies only one of four fixed slots. Read identity from the sealed guest input.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from openscience_per_user_dev import inspect_quota_volume

    users = json.loads(Path("/inputs/spec.json").read_bytes())["users"]
    owner = users[slot[-1]]
    binding = {k: owner[k] for k in ("environment", "user_id", "workspace", "uid")}
    mount = Path(GUEST_ROOT) / slot
    serial = ("m5-" if slot.startswith("src-") else "m5-restore-") + slot[-1]
    disk_pins = json.loads(Path("/etc/nexpoly-m5-disks.json").read_bytes())
    binding.update(mount=str(mount), filesystem_uuid=disk_pins[serial])
    service_root = Path("/opt/nexpoly-m5-nfs")
    exports = runpy.run_path(str(service_root / "guest.py"))
    rows = {s: {"mount": str(Path(GUEST_ROOT) / s), "pseudo": "/m5-" + s, "uid": users[s[-1]]["uid"]} for s in SLOTS}
    config = service_root / "ganesha.conf"
    if config.read_text() != exports["export_config"](rows):
        raise ValueError("Configured NFS exports differ from the four registered quota disks")
    pid = subprocess.check_output(["systemctl", "show", "m5-native-nfs.service", "-p", "MainPID", "--value"], text=True, timeout=5).strip()
    if not pid.isdecimal() or int(pid) <= 1:
        raise ValueError("The private NFS server is unavailable")
    process = Path("/proc") / pid
    argv = (process / "cmdline").read_bytes().split(b"\0")[:-1]
    if (argv != [os.fsencode(service_root / "root/usr/bin/ganesha.nfsd"), b"-F", b"-f", os.fsencode(config), b"-L", b"SYSLOG"]
            or not (process / "root" / str(mount).lstrip("/")).samefile(mount)):
        raise ValueError("Live private NFS server or its exported filesystem differs")
    quota = inspect_quota_volume({**binding, "generation": owner["registered_generation"]})
    return {"binding": binding, "quota": quota}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("attest", "attest-owned", "claim", "check-claim"):
        item = commands.add_parser(name)
        if name.startswith("attest"):
            item.add_argument("slot", choices=SLOTS)
        else:
            item.add_argument("phase", choices=("src", "dst"))
        if name != "attest":
            item.add_argument("deployment")
            item.add_argument("root_hash")
        if name in ("claim", "check-claim"):
            item.add_argument("uuids", nargs=2)
    args = parser.parse_args()
    if args.command.startswith("attest"):
        result = guest_attestation(args.slot)
        if args.command == "attest-owned":
            result["claim"] = pinned_guest_claim(args.slot[:3], args.deployment, args.root_hash, acquire=False)
    else:
        result = pinned_guest_claim(args.phase, args.deployment, args.root_hash, acquire=args.command == "claim",
                                    supplied_uuids=args.uuids)
    print(json.dumps(result, sort_keys=True))

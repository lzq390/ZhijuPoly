"""Provision four loopback-only Ganesha exports in the dedicated M5 quota VM."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path("/opt/nexpoly-m5-nfs")
LIB = ROOT / "root/usr/lib"
MOUNTS = Path("/mnt/openscience-m5-quota")
SERVICE = "m5-native-nfs.service"
SLOTS = ("src-a", "src-b", "dst-a", "dst-b")
# The first dev VM enumerated metadata() in insertion order. Its four export
# IDs match the subsequent checked-in driver and the exact legacy config below.
LEGACY_DRIVERS = {
    "821bebc34f3746533fab68454ec99236af4a2af7833585234b0880babbcaeade",
    "33be08374d79a495c90efdf8cccb9db9c208e5ca08f5336700ff348fa12ad9cf",
}


def metadata():
    sys.path.insert(0, "/inputs")
    import quota

    spec = json.loads(Path("/inputs/spec.json").read_bytes())
    pins = json.loads(Path("/etc/nexpoly-m5-disks.json").read_bytes())
    rows = {}
    for slot in SLOTS:
        stage, role = slot.split("-")
        owner = spec["users"][role]
        disk = MOUNTS / slot
        observed = quota.read(disk, owner)
        serial = ("m5-" if stage == "src" else "m5-restore-") + role
        if (
            observed["filesystem_uuid"] != pins[serial]
            or observed["volume_label"] != "m5quota-" + slot
        ):
            raise ValueError("Expected the pinned dedicated ext4 mount for " + slot)
        rows[slot] = {
            **{k: owner[k] for k in ("environment", "user_id", "workspace", "uid")},
            "generation": owner["registered_generation"],
            "mount": str(disk),
            "filesystem_uuid": pins[serial],
            "pseudo": "/m5-" + slot,
        }
    return rows


def export_config(rows, *, legacy=False):
    config = """NFS_CORE_PARAM {
 NFS_Port = 2049;
 Bind_addr = 127.0.0.1;
 Protocols = 4;
 Enable_NLM = false;
 Enable_RQUOTA = false;
 Plugins_Dir = /opt/nexpoly-m5-nfs/root/usr/lib/x86_64-linux-gnu/ganesha;
}
"""
    # The pilot also edits unmounted ext4 volumes during maintenance. A cached
    # READDIR chunk can outlive those edits and refer to a deleted inode.
    if not legacy:
        config += "MDCACHE { Dir_Chunk = 0; }\n"
    # One v4.1 forechannel slot orders OPEN/CLOSE on the pilot's loopback
    # Ganesha server. The ext4 quota disks and the registered mounts stay the same.
    slots = "" if legacy else "Slot_Table_Size = 1; "
    config += "NFSv4 { " + slots + "Minor_Versions = 1; Graceless = true; Delegations = false; Only_Numeric_Owners = true; }\n"
    attributes = "" if legacy else " Attr_Expiration_Time = 0;\n"
    for export_id, slot in enumerate(SLOTS, 101):
        row = rows[slot]
        config += f"""EXPORT {{
 Export_Id = {export_id};
 Path = {row["mount"]};
 Pseudo = {row["pseudo"]};
{attributes} Access_Type = NONE;
 Squash = Root_Squash;
 Anonymous_Uid = {row["uid"]};
 Anonymous_Gid = {row["uid"]};
 Protocols = 4;
 Transports = TCP;
 SecType = sys;
 FSAL {{ Name = VFS; }}
 CLIENT {{ Clients = 127.0.0.1; Access_Type = RW; Squash = Root_Squash; }}
}}
"""
    return config


def binding_digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def publish(path, content, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(tmp, path)
            directory = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            tmp.unlink(missing_ok=True)


def start_exports():
    subprocess.run(["systemctl", "start", SERVICE], check=True)
    deadline = time.monotonic() + 30
    while True:
        subprocess.run(["systemctl", "is-active", "--quiet", SERVICE], check=True)
        try:
            with socket.create_connection(("127.0.0.1", 2049), timeout=1):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def refresh(expected):
    rows = metadata()
    if binding_digest(rows) != expected:
        raise ValueError("Retained ext4 bindings differ from the host manifest")
    driver, config = ROOT / "guest.py", ROOT / "ganesha.conf"
    for path in (driver, config):
        if path.resolve(strict=True) != path or not path.is_file():
            raise ValueError("Unexpected retained export file")
    old_driver, old_config = driver.read_bytes(), config.read_bytes()
    new_driver = Path(__file__).read_bytes()
    new_config = export_config(rows).encode()
    if old_driver != new_driver and hashlib.sha256(old_driver).hexdigest() not in LEGACY_DRIVERS:
        raise ValueError("Unrecognized retained export driver")
    if old_config not in (new_config, export_config(rows, legacy=True).encode()):
        raise ValueError("Unrecognized retained export configuration")
    # The host has reserved the relay port and checked all four Docker mounts.
    # Always restart: an interrupted previous invocation may have published
    # both files just before applying the new configuration to the service.
    subprocess.run(["systemctl", "stop", SERVICE], check=True)
    try:
        publish(driver, new_driver, 0o600)
        publish(config, new_config, 0o600)
        start_exports()
    except BaseException:
        subprocess.run(["systemctl", "stop", SERVICE], check=True)
        publish(config, old_config, 0o600)
        publish(driver, old_driver, 0o600)
        start_exports()
        raise
    return rows


def install():
    rows = metadata()
    packages = json.loads((ROOT / "packages.json").read_bytes())
    for source, sha in packages.items():
        deb = ROOT / "debs" / Path(source).name
        if hashlib.sha256(deb.read_bytes()).hexdigest() != sha:
            raise ValueError("Unexpected NFS binary package")
        subprocess.run(["dpkg-deb", "-x", deb, ROOT / "root"], check=True)
    # nfsidmap loads its nsswitch plugin through a compiled-in path.
    plugins = Path("/usr/lib/x86_64-linux-gnu/libnfsidmap")
    if not plugins.exists():
        plugins.symlink_to(LIB / "x86_64-linux-gnu/libnfsidmap")
    if not (plugins / "nsswitch.so").is_file():
        raise ValueError("NFS numeric UID mapping support is missing")
    config = export_config(rows)
    target = ROOT / "ganesha.conf"
    if target.exists() and target.read_text() != config:
        raise ValueError("Retained NFS exports differ from the verified M5 bindings")
    target.write_text(config)
    target.chmod(0o600)
    for path in ("/run/ganesha", "/var/lib/nfs/ganesha"):
        Path(path).mkdir(parents=True, exist_ok=True)
    (Path("/etc/systemd/system") / SERVICE).write_text(
        "[Unit]\nDescription=Export the four isolated M5 quota disks on guest loopback\n"
        "Requires=m5-storage.service\nAfter=m5-storage.service\n"
        "[Service]\nRuntimeDirectory=ganesha\n"
        f"Environment=LD_LIBRARY_PATH={LIB}/ganesha:{LIB}/x86_64-linux-gnu\n"
        f"ExecStartPre=/usr/bin/python3 -IB {ROOT}/guest.py status\n"
        f"ExecStart={ROOT}/root/usr/bin/ganesha.nfsd -F -f {target} -L SYSLOG\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", SERVICE], check=True)
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "start", "status"))
    action = parser.parse_args().action
    if action == "start":
        start_exports()
    else:
        print(json.dumps(install() if action == "install" else metadata(), sort_keys=True))

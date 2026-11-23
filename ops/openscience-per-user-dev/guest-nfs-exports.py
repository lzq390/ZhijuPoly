"""Provision four loopback-only Ganesha exports in the dedicated M5 quota VM."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path("/opt/nexpoly-m5-nfs")
LIB = ROOT / "root/usr/lib"
MOUNTS = Path("/mnt/openscience-m5-quota")
SERVICE = "m5-native-nfs.service"
SLOTS = ("src-a", "src-b", "dst-a", "dst-b")


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


def export_config(rows):
    config = """NFS_CORE_PARAM {
 NFS_Port = 2049;
 Bind_addr = 127.0.0.1;
 Protocols = 4;
 Enable_NLM = false;
 Enable_RQUOTA = false;
 Plugins_Dir = /opt/nexpoly-m5-nfs/root/usr/lib/x86_64-linux-gnu/ganesha;
}
NFSv4 { Minor_Versions = 1; Graceless = true; Delegations = false; Only_Numeric_Owners = true; }
"""
    for export_id, slot in enumerate(SLOTS, 101):
        row = rows[slot]
        config += f"""EXPORT {{
 Export_Id = {export_id};
 Path = {row["mount"]};
 Pseudo = {row["pseudo"]};
 Access_Type = NONE;
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
    parser.add_argument("action", choices=("install", "status"))
    print(
        json.dumps(
            install() if parser.parse_args().action == "install" else metadata(),
            sort_keys=True,
        )
    )

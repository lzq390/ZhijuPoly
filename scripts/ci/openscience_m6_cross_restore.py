"""Stream a quiesced deploy-m6 checkpoint to a separately provisioned restore target.

On the source, export writes a tar stream to stdout. Send that stream over pinned
SSH to the target's import command. Supply the trusted source checkpoint report
to both commands separately. Import writes the verified receipt to stdout.
Use connect with that receipt to publish and check the destination NFS volumes.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import start_openscience_m5_web_vm as launcher
from openscience_m6_candidate_storage import connect
from openscience_m5_backup import private_file
from openscience_m5_storage_ssh import StorageGuest


def verify_receipt(result, evidence):
    checkpoint = evidence["web_checkpoint"]
    if (result["status"] != "passed" or result["checkpoint"] != checkpoint["generation"]
            or set(result["accounts"]) != {"a", "b"}):
        raise ValueError("Unexpected cross-host restore receipt")
    for role, restored in result["accounts"].items():
        source = evidence["accounts"][role]
        cold = checkpoint["accounts"][role]
        expected = {"inventory": cold["source_inventory"], "manifest_sha256": cold["manifest_sha256"],
                    "source_uuid": source["source"]["filesystem_uuid"], "hard_bytes": source["source"]["hard_bytes"],
                    "uid": source["owner"]["uid"], "gid": source["owner"]["gid"], "accounting": True, "enforced": True}
        if any(restored[key] != value for key, value in expected.items()):
            raise ValueError("Target recovery differs from the trusted source checkpoint")
    uuids = {row[key] for row in result["accounts"].values() for key in ("source_uuid", "destination_uuid")}
    if len(uuids) != 4:
        raise ValueError("Each source and destination filesystem must be distinct")


def transfer(vm, report, action, receipt, packages):
    with private_file(report) as stream:
        evidence = json.load(stream)
    checkpoint = evidence["web_checkpoint"]
    generation = checkpoint["generation"]
    accounts = checkpoint["accounts"]
    if (evidence["format"] != "nexpoly-current-host-storage-v1" or checkpoint["status"] != "passed"
            or not re.fullmatch(r"[a-f0-9]{32}", generation) or set(accounts) != {"a", "b"}):
        raise ValueError("Expected a verified trusted candidate checkpoint report")
    pins = [accounts[role]["manifest_sha256"] for role in "ab"]
    if any(not re.fullmatch(r"[a-f0-9]{64}", pin) for pin in pins):
        raise ValueError("Expected the two source manifest hashes")
    guest = StorageGuest(vm / "ssh.conf")
    config = json.loads((vm / "vm.json").read_bytes())
    with launcher.storage_lock(vm):
        launcher.checked_layout(vm, config)
        if action == "export":
            if config.get("restore_target") or "backup_dir" not in config:
                raise ValueError("Expected the registered checkpoint source")
            names = [f"{role}/{name}" for role in "ab" for name in ("manifest.json", "workspace.tar")]
            guest.run("tar", "-C", "/backup/checkpoint-" + generation, "-cf", "-", "--", *names,
                      stdout=sys.stdout.buffer)
            return
        if config.get("restore_target") is not True:
            raise ValueError("Expected a separately provisioned restore target")
        if action == "connect":
            with private_file(receipt) as stream:
                restored = json.load(stream)
            verify_receipt(restored, evidence)
            disks = json.loads(guest.run("cat", "/etc/nexpoly-m5-disks.json", capture_output=True).stdout)
            if any(disks["m5-restore-" + r] != restored["accounts"][r]["destination_uuid"] for r in "ab"):
                raise ValueError("Receipt belongs to another target")
            connect(vm, guest, packages)
            print((vm / "native/dst-bindings.json").read_text())
            return
        script = Path(__file__).resolve().parents[2] / "ops/openscience-per-user-dev/quota-candidate-import.py"
        program = "/run/nexpoly-m6-checkpoint-import.py"
        guest.run("install", "-m", "0600", "/dev/stdin", program, input=script.read_bytes())
        result = json.loads(guest.run("python3", "-IB", program, generation, *pins,
                                      stdin=sys.stdin.buffer, stdout=subprocess.PIPE).stdout)
        verify_receipt(result, evidence)
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "import", "connect"))
    parser.add_argument("--vm", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, help="Verified destination receipt from import, required for connect")
    parser.add_argument("--packages-dir", type=Path, help="Offline pinned NFS packages for connect")
    args = parser.parse_args()
    if (args.action == "connect") != bool(args.receipt) or (args.packages_dir and args.action != "connect"):
        parser.error("Use --receipt and the optional --packages-dir only with connect")
    os.umask(0o077)
    transfer(args.vm.resolve(strict=True), args.report, args.action, args.receipt, args.packages_dir)

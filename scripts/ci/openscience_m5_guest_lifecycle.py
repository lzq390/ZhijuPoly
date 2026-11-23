"""Inspect, fill and restore ext4 on the storage side of the M5 browser drill."""

import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "ops/openscience-per-user-dev"
sys.path[:0] = [str(ROOT / "scripts"), str(OPS)]
from openscience_m5_quota_bundle import backup, restore, inventory, quota  # noqa: E402

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


def recover(spec):
    packages = Path("/backup") / "web"
    require(Path("/backup").is_mount(), "Expected dedicated backup disk")
    packages.mkdir(mode=0o700, exist_ok=True)
    results = {}
    for role, owner in spec["users"].items():
        source, target = (mount(phase, role) for phase in ("src", "dst"))
        source_uuid = quota.read(source, owner)["filesystem_uuid"]
        before = quota.read(target, owner)
        require(
            before["hard_bytes"] == 0 and not (target / "workspace").exists(),
            "Expected empty restore volume",
        )
        saved = backup(source, owner, packages / role)
        recovered = restore(target, owner, packages / role, saved["manifest_sha256"])
        restored_file = notebook("dst", role, owner)
        require(
            recovered["inventory"] == saved["inventory"]
            and restored_file == notebook("src", role, owner)
            and recovered["quota"]["hard_bytes"] == guest["LIMIT"]
            and recovered["quota"]["enforced"]
            and recovered["quota"]["filesystem_uuid"] != source_uuid,
            "Quota, ownership or content differs after restore",
        )
        results[role] = dict(
            empty_target=before,
            backup=saved,
            restored=recovered,
            notebook=restored_file,
        )
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "step", choices=("prepare", "inspect", "fill", "unfill", "recover")
    )
    parser.add_argument("--phase", choices=("src", "dst"), default="src")
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
        result = recover({"users": users})
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

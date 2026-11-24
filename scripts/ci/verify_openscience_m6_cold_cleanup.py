"""Coordinate the two-browser cold cleanup drill on the retained dev-m1 quota test pool."""

import argparse
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

from verify_openscience_m5_watermark import MIB, OWNERS, ROOT, Trial, json_write, wait_file

# Place one non-sparse file under A's real unprivileged UID. The 96 MiB allocation
# lets the browser deletion alone take the pool from below 96 to above 160 MiB.
FIXTURE = """
import hashlib, json, os, stat, sys
from pathlib import Path
sys.path[:0] = ['/opt/nexpoly/scripts','/opt/nexpoly/ops/openscience-per-user-dev','/inputs']
import water_pool as pool
from quota_guest import run_as
action, tag, project, fs, uid = sys.argv[1:]
cfg, roles = pool.topology()
state = pool.observation(cfg, roles)['workspaces']['a']
assert state['uid'] == int(uid) and state['filesystem_uuid'] == fs
folder = pool.ROOTS / 'src-a/workspace/files/projects' / project
binding = json.loads(Path('/inputs/spec.json').read_bytes())['users']['a']
assert binding['uid'] == int(uid)
def file():
    path = folder / ('release-' + tag + '.txt')
    block = (('A ' + tag + ' release').encode().ljust(1023, b'.') + b'\\n') * 1024
    sha = hashlib.sha256()
    for _ in range(96):
        sha.update(block)
    if action == 'create':
        with path.open('xb') as f:
            for _ in range(96):
                f.write(block)
            f.flush()
            os.fsync(f.fileno())
    elif not path.exists():
        return {'absent': True}
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == int(uid)
    assert info.st_size == len(block) * 96 and info.st_blocks * 512 >= info.st_size
    with path.open('rb') as f:
        observed = hashlib.sha256()
        while chunk := f.read(1024*1024):
            observed.update(chunk)
    assert observed.digest() == sha.digest()
    if action == 'cleanup':
        path.unlink()
    return {'size': info.st_size, 'allocated_bytes': info.st_blocks * 512, 'sha256': sha.hexdigest()}
print(json.dumps(run_as(binding, file)))
"""


def bound_rows(trial, action):
    rows = {}
    for user in OWNERS:
        r = trial.workspace(action, user)
        rows[user] = {k: r[k] for k in ("state", "generation", "uid", "volume", "workspace")}
        rows[user]["volume_holders"] = len(subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", "volume=" + r["volume"]]).splitlines())
    return rows


def drill(args):
    t = Trial(args)
    stack = json.loads((args.state / "running.json").read_bytes())
    assert stack["environment"] == "dev-m1" and stack["quota_files"] and Path(stack["registry"]) == t.registry
    ac22 = json.loads((ROOT / "docs/verification/openscience-m6-ac22-20261124.json").read_bytes())
    initial = bound_rows(t, "status")
    for user, row in initial.items():
        assert row["state"] == "recycled" and row["volume_holders"] == 0
        assert all(row[k] == ac22["runtime"]["dev-m1"][user][k] for k in ("uid", "workspace", "volume"))
    first = t.wait_observed("normal", 300 * MIB)
    assert (first["low_bytes"], first["recover_bytes"]) == (96 * MIB, 160 * MIB)
    sample = t.pool("sample")
    assert first["filesystem_uuid"] == sample["filesystem_uuid"]
    guest_work = sample["workspaces"]["a"]
    assert guest_work["uid"] == initial[OWNERS[0]]["uid"]
    tag = uuid4().hex[:12]
    def fixture(action):
        return json.loads(t.guest.run("python3", "-IBc", FIXTURE, action, tag, ac22["project"],
                                      guest_work["filesystem_uuid"], str(guest_work["uid"]),
                                      capture_output=True, timeout=120).stdout)
    t.guest.run("test", "!", "-e", "/mnt/nexpoly-m5-pool/.m5-global-drill/pressure.dat", timeout=20)
    data = {"tag": tag, "project": ac22["project"], "initial": initial}
    json_write(args.work / "fixture.json", data)
    history = t.alerts()
    report = {"environment": "dev-m1", "pool": sample["filesystem_uuid"], "initial": initial,
              "before": first, "fixture": fixture("create")}
    pressure = False
    try:
        with (args.work / "browser.log").open("x") as log:
            proc = subprocess.Popen(["node", ROOT / "scripts/ci/verify_openscience_m6_cold_cleanup.mjs",
                                     args.state, args.work], stdout=log, stderr=subprocess.STDOUT)
            try:
                wait_file(args.work / "prepared", proc, 450)
                report["cold"] = bound_rows(t, "recycle")
                assert all(r["state"] == "recycled" and not r["volume_holders"] for r in report["cold"].values())
                pressure = True
                t.pool("pressure", "80")
                report["low"] = t.wait_observed("low", maximum=96 * MIB)
                assert report["low"]["available_bytes"] < 96 * MIB
                t.signal("low")
                wait_file(args.work / "checked", proc, 420)
                report["before_unlink"] = t.wait_observed("low", maximum=96 * MIB)
                report["checked_cold"] = bound_rows(t, "status")
                assert report["checked_cold"] == report["cold"]
                report["filler_before"] = t.filler_extent()
                t.signal("delete")
                wait_file(args.work / "deleted", proc, 150)
                report["recovered"] = t.wait_observed("normal", 160 * MIB)
                report["filler_after"] = t.filler_extent()
                assert report["filler_after"] == report["filler_before"]
                report["after_unlink"] = bound_rows(t, "status")
                assert report["after_unlink"] == report["cold"]
                json_write(args.work / "watermark.json", report)
                t.signal("restart")
                if proc.wait(timeout=450):
                    raise RuntimeError("Browser recovery failed, see browser.log")
                report["restarted"] = bound_rows(t, "status")
                for user, row in report["restarted"].items():
                    cold = report["cold"][user]
                    assert row["state"] == "running" and row["generation"] == cold["generation"] + 1
                    assert all(row[k] == cold[k] for k in ("uid", "workspace", "volume"))
                report["alerts"] = [a for a in t.alerts() if a not in history]
                assert [a["state"] for a in report["alerts"]] == ["low", "normal"]
                assert fixture("check") == {"absent": True}
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=20)
    finally:
        if pressure:
            t.pool("release")
            t.wait_observed("normal", 160 * MIB)
        report["parked"] = bound_rows(t, "recycle")
        assert all(r["state"] == "recycled" and not r["volume_holders"] for r in report["parked"].values())
        fixture("cleanup")
    report["browser"] = json.loads((args.work / "browser.json").read_bytes())
    assert report["browser"]["status"] == "passed"
    report["status"] = "passed"
    json_write(args.work / "verified.json", report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/state/nexpoly-openscience-m2-dev")
    parser.add_argument("--registry-root", type=Path, default=Path.home() / ".local/state/nexpoly-m5-native-01")
    parser.add_argument("--vm", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    options = parser.parse_args()
    os.umask(0o077)
    options.work.mkdir(mode=0o700)
    drill(options)

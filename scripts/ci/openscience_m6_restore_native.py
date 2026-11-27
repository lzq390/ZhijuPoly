"""Seed or verify native project transcripts and attachments for the two-guest restore rehearsal."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

import verify_openscience_per_user_dev as api

sys.path.insert(0, str(api.ROOT / "scripts"))
import openscience_m5_backup as backup
import openscience_per_user_dev as m1


PROJECT = "native-restored-project"
DIRECTORY = f"{api.PROJECTS}/{PROJECT}"
PILOTS = json.loads((api.ROOT / "docs/verification/openscience-m6-ac22-20261124.json").read_text())["runtime"]["dev-m1"]
ROLES = ("a", "b")
IMAGE = PILOTS["test-user-a"]["image"]


@contextmanager
def fresh_workspaces():
    # Only the identity numbers come from recorded pilot evidence. Docker names use a new deployment.
    with TemporaryDirectory(prefix="m6-native-registry-") as temp:
        state = Path(temp)

        def manager(*args):
            return json.loads(api.run(sys.executable, str(api.MANAGER), "--state-dir", temp, *args))

        deployment = manager("init", "--image", IMAGE)["deployment"]
        registry = m1.Registry(state)
        api.check(registry.data["workspaces"] == {}, "fresh registry has existing bindings")
        workspaces = {}
        for role in ROLES:
            user = "test-user-" + role
            pilot = PILOTS[user]
            api.check(pilot["image"] == IMAGE and pilot["uid"] == pilot["gid"], "pilot binding differs")
            registry.data["workspaces"]["dev-m1/" + user] = m1.new_row(pilot["workspace"], pilot["uid"])
            workspaces[role] = registry.workspace("dev-m1", user)
            api.check(workspaces[role].resource("volume") is None, "disposable volume name exists")
        registry.save()

        def act(verb, role):
            return manager(verb, "--environment", "dev-m1", "--user-id", "test-user-" + role)

        try:
            yield deployment, workspaces, act
        finally:
            owned = api.generated_resources(deployment)
            if owned["containers"]:
                api.run("docker", "rm", "-f", *owned["containers"])
            if owned["volumes"]:
                api.run("docker", "volume", "rm", *owned["volumes"])
            api.check(not any(api.generated_resources(deployment).values()), "disposable resources remain")


def seed(binding, role, marker):
    project = api.request(binding, "/api/resolve-folder/create", "POST", {"parent": api.PROJECTS, "name": PROJECT})
    api.check(project["ok"] and project["absolute"] == DIRECTORY, "project creation failed")
    filename = f"attachment-{role}.txt"
    api.request(binding, "/file/content", "PUT", {"path": filename, "content": marker + " attachment"}, directory=DIRECTORY)
    session = api.request(binding, "/session", "POST", {"title": marker}, directory=DIRECTORY)
    parts = [{"type": "text", "text": marker + " prompt"},
             {"type": "file", "mime": "text/plain", "filename": filename, "url": f"file://{DIRECTORY}/{filename}"}]
    reply = api.request(binding, f"/session/{session['id']}/message", "POST",
                        {"model": {"providerID": "m0fixture", "modelID": "m0"}, "parts": parts}, directory=DIRECTORY)
    api.check(any(part.get("text") == api.MODEL_REPLY for part in reply["parts"]), "fixture reply missing")
    return session["id"]


def snapshot(binding, role, run_id, ids):
    other = "b" if role == "a" else "a"
    own, foreign = f"{run_id}-{role}", f"{run_id}-{other}"
    filename = f"attachment-{role}.txt"
    projects = api.request(binding, "/api/resolve-folder/list")["entries"]
    api.check(PROJECT in [p["name"] for p in projects], "native project missing")
    sessions = api.sessions(binding, DIRECTORY)
    api.check([(s["id"], s["title"]) for s in sessions] == [(ids[role], own)], "unexpected native sessions")
    status = api.request(binding, f"/session/{ids[other]}", directory=DIRECTORY, expected_status=404)
    messages = api.request(binding, f"/session/{ids[role]}/message", directory=DIRECTORY)
    api.check(foreign not in json.dumps(messages), "foreign marker in transcript")
    parts = {m["info"]["role"]: m["parts"] for m in messages}
    api.check(any(p.get("text") == own + " prompt" for p in parts["user"]), "own prompt missing")
    api.check(any(p.get("text") == api.MODEL_REPLY for p in parts["assistant"]), "own reply missing")
    api.check(any(p["type"] == "file" and p.get("filename") == filename and p.get("mime") == "text/plain"
                  for p in parts["user"]), "attachment reference missing")
    files = {f["name"] for f in api.request(binding, "/file", directory=DIRECTORY, path="")}
    other_file = f"attachment-{other}.txt"
    api.check(filename in files and other_file not in files, "attachment listing differs")
    content = api.request(binding, "/file/content", directory=DIRECTORY, path=filename)["content"]
    api.check(content == own + " attachment", "attachment bytes differ")
    # OpenScience returns an empty content envelope for a missing file.
    missing = api.request(binding, "/file/content", directory=DIRECTORY, path=other_file)
    api.check(missing == {"type": "text", "content": ""}, "foreign attachment can be read by name")
    return {"project": PROJECT, "session_id": ids[role], "transcript_sha256": api.transcript_digest(messages),
            "attachment": filename, "attachment_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "other_session_http": status, "other_attachment_absent": True}


def unpack(workspace, path, expected):
    m1.docker("volume", "create", *workspace.label_flags("volume"), workspace.volume)
    api.check(workspace.resource("volume") == workspace.volume, "unverified restore volume")
    with path.open("rb") as source:
        api.check(backup.digest(source) == expected["export_sha256"], "target archive checksum differs from VM receipt")
        source.seek(0)
        inventory = json.loads(subprocess.run(backup.helper(workspace, "unpack"), stdin=source, stdout=subprocess.PIPE,
                                              check=True, timeout=120).stdout)
    api.check(inventory == expected["inventory"], "restored inventory differs from VM receipt")
    return inventory


def pack(workspace, path):
    backup.is_unmounted(workspace)
    with path.open("xb") as output:
        subprocess.run(backup.helper(workspace, "pack"), stdout=output, check=True, timeout=120)
    with path.open("rb") as source:
        return backup.process_archive(source, int(workspace.uid), workspace.row["id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed").add_argument("output", type=Path, help="new directory for a.tar, b.tar, and report.json")
    verify = commands.add_parser("verify")
    verify.add_argument("source", type=Path, help="report.json from seed")
    verify.add_argument("rehearsal", type=Path, help="JSON receipt from the two-guest restore")
    verify.add_argument("restored", type=Path, help="complete A/B packages exported by the target guest")
    verify.add_argument("output", type=Path, help="new results directory")
    args = parser.parse_args()
    os.umask(0o077)
    os.environ.pop("OPENSCIENCE_M3_OVERLAY", None)
    args.output.mkdir(mode=0o700)
    restoring = args.command == "verify"
    before = json.loads(args.source.read_text()) if restoring else {}
    run_id = before["run_id"] if restoring else "M6-" + uuid4().hex
    with fresh_workspaces() as (deployment, workspaces, act):
        report = {"phase": args.command, "run_id": run_id, "image": IMAGE, "deployment": deployment}
        if restoring:
            api.check(before["image"] == IMAGE and before["deployment"] != deployment, "source identity mismatch")
            receipt = args.rehearsal.read_bytes()
            guests = json.loads(receipt)
            api.check(guests["status"] == "passed" and guests["source_disks_deleted_before_target_boot"]
                      and guests["temporary_disks_removed"], "two-guest rehearsal did not complete")
            report["rehearsal_sha256"] = hashlib.sha256(receipt).hexdigest()
            report["source"] = {field: before[field] for field in ("deployment", "runtime", "data", "archives")}
            report["archives"] = {}
            for role in ROLES:
                with (args.source.parent / (role + ".tar")).open("rb") as source:
                    api.check(backup.digest(source) == guests["source"]["native_fixtures_sha256"][role],
                              "source archive differs from input captured by source guest")
                report["archives"][role] = unpack(workspaces[role], args.restored / (role + ".tar"),
                                                  guests["destination"]["accounts"][role])
        bindings = api.together(ROLES, lambda r: act("ensure", r))
        report["runtime"] = {r: api.inspect_runtime(b, deployment, IMAGE) for r, b in bindings.items()}
        ids = ({r: before["data"][r]["session_id"] for r in ROLES} if restoring else
               api.together(ROLES, lambda r: seed(bindings[r], r, run_id + "-" + r)))
        api.check(ids["a"] != ids["b"], "duplicate native session IDs")
        report["data"] = api.together(ROLES, lambda r: snapshot(bindings[r], r, run_id, ids))
        if restoring:
            api.check(report["data"] == before["data"], "native API data changed during transfer")
        else:
            for role in ROLES:
                api.check(act("recycle", role)["state"] == "recycled", "source still active")
            report["archives"] = {r: pack(workspaces[r], args.output / (r + ".tar")) for r in ROLES}
    report["remaining_disposable_resources"] = 0
    report["status"] = "passed"
    (args.output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

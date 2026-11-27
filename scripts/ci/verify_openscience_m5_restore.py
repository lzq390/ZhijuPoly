"""Compare retained dev-m1 records read-only with two newly restored workspaces through their APIs."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import verify_openscience_per_user_dev as m1

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openscience_per_user_dev as supervisor
from openscience_m5_backup import offline_registry


USERS = ("test-user-a", "test-user-b")
ENVIRONMENT = "dev-m1"
DIRECTORIES = {"general": m1.GENERAL, "project": m1.DIRECTORY}

# Reads only M1 acceptance records. Output contains identifiers, counts and digests.
SOURCE_RECORDS = """
import hashlib, json, sys
from pathlib import Path
owner = sys.argv[1]
root = Path('/data')
def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
sessions = {}
for path in (root / 'records/projects').glob('*/sessions/*/session.json'):
    session = json.loads(path.read_bytes())
    for kind in ('general', 'project'):
        if session.get('title') != 'M1 synthetic ' + owner + ' ' + kind:
            continue
        assert kind not in sessions and session['id'] == path.parent.name
        messages = []
        for message in sorted((path.parent / 'messages').glob('*.json')):
            info = json.loads(message.read_bytes())
            parts = [json.loads(p.read_bytes()) for p in sorted((path.parent / 'parts' / info['id']).glob('*.json'))]
            messages.append({'info': info, 'parts': parts})
        assert messages
        sessions[kind] = {'id': session['id'], 'messages': len(messages), 'sha256': digest(messages)}
assert set(sessions) == {'project', 'general'}
file = (root / 'files/projects/same-project/notebook.txt').read_bytes()
print(json.dumps({'sessions': sessions, 'notebook_sha256': hashlib.sha256(file).hexdigest()}))
"""

RETIRED_TOKEN = """
import sys, urllib.error, urllib.request
old = sys.stdin.read().strip()
req = urllib.request.Request('http://127.0.0.1:4096/session', headers={'Authorization': 'Bearer ' + old})
try:
    with urllib.request.urlopen(req, timeout=5) as response:
        print(response.status)
except urllib.error.HTTPError as error:
    print(error.code)
"""


def offline_baseline(source):
    with offline_registry(source) as registry:
        rows = {}
        for user in USERS:
            w = registry.workspace(ENVIRONMENT, user)
            m1.check(not w.resource("backend") and not w.resource("fixture") and w.resource("volume"),
                     "Original workspace must already be offline on its registered volume")
            records = json.loads(w.helper(SOURCE_RECORDS, user))
            rows[user] = {**records, "binding": w.state()}
        return registry.data["image"], rows


def check_api(row, original, other):
    projects = m1.request(row, "/api/resolve-folder/list")
    m1.check(any(p["name"] == m1.PROJECT for p in projects["entries"]), "Missing restored project")
    content = m1.request(row, "/file/content", directory=m1.DIRECTORY, path=m1.FILE)["content"]
    sha = hashlib.sha256(content.encode()).hexdigest()
    m1.check(sha == original["notebook_sha256"] != other["notebook_sha256"], "Restored attachment differs")
    observed = {}
    for kind, directory in DIRECTORIES.items():
        before = original["sessions"][kind]
        foreign = other["sessions"][kind]["id"]
        listed = {s["id"] for s in m1.sessions(row, directory)}
        m1.check(before["id"] in listed and foreign not in listed, "Unexpected session ownership")
        messages = m1.request(row, f"/session/{before['id']}/message", directory=directory)
        ordered = [{"info": m["info"], "parts": sorted(m["parts"], key=lambda part: part["id"])}
                   for m in sorted(messages, key=lambda item: item["info"]["id"])]
        m1.check(len(ordered) == before["messages"] and m1.transcript_digest(ordered) == before["sha256"],
                 "Restored transcript differs from original offline records")
        if kind == "project":
            m1.check(any(p.get("type") == "file" and p.get("filename") == m1.FILE
                         for m in ordered for p in m["parts"]), "Restored attachment reference missing")
        status = m1.request(row, f"/session/{foreign}", expected_status=404, directory=directory)
        observed[kind] = {**before, "other_session_http": status}
    return {"sessions": observed, "notebook_sha256": sha, "attachment_reference_matches": True}


def verify(source, target):
    supervisor.local_engine()
    image, originals = offline_baseline(source)
    with supervisor.locked_registry(target, False) as registry:
        m1.check({(w.env, w.user) for w in registry.bindings()} == {(ENVIRONMENT, u) for u in USERS},
                 "Expected a dedicated restored A/B registry")
        m1.check(registry.data["image"] == image, "Restored image differs from the backed up template")
        starters = {}
        for user in USERS:
            w = registry.workspace(ENVIRONMENT, user)
            src = originals[user]["binding"]
            before = w.state()
            m1.check(before["deployment"] != src["deployment"] and before["volume"] != src["volume"]
                     and all(before[k] == src[k] for k in ("uid", "gid", "workspace", "generation"))
                     and not w.resource("backend") and not w.resource("fixture")
                     and w.resource("volume") and not w.secret_dir.exists(),
                     "Expected a new offline restore with original ownership and fresh credential path")
            starters[user] = before
    def action(command, user):
        return json.loads(m1.run(sys.executable, str(m1.MANAGER), "--state-dir", str(target),
                                 command, "--environment", ENVIRONMENT, "--user-id", user))
    result = {}
    started = []
    try:
        first_tokens = {}
        for user, other in zip(USERS, reversed(USERS)):
            row = action("ensure", user)
            started.append(user)
            m1.inspect_runtime(row, row["deployment"], image)
            m1.check(row["generation"] == starters[user]["generation"] + 1, "Generation did not advance")
            result[user] = {**check_api(row, originals[user], originals[other]),
                            "workspace": row["workspace"], "uid": row["uid"], "gid": row["gid"],
                            "volume_root_mode": "0700", "source_generation": starters[user]["generation"],
                            "first_restored_generation": row["generation"]}
            first_tokens[user] = (target / row["workspace"] / "secret/token").read_bytes()
        for user in USERS:
            m1.check(action("recycle", user)["state"] == "recycled", "Could not quiesce restored workspace")
            row = action("ensure", user)
            status = subprocess.run(["docker", "exec", "-i", row["fixture"], "python", "-Bc", RETIRED_TOKEN],
                                    input=first_tokens.pop(user), capture_output=True, check=True, timeout=15)
            m1.check(status.stdout.strip() == b"401", "Prior restored credential retained access")
            own = originals[user]["sessions"]["project"]["id"]
            m1.check(m1.request(row, f"/session/{own}", directory=m1.DIRECTORY)["id"] == own,
                     "New generation lost access to restored project session")
            result[user].update(reissued_credential_reads=True, previous_credential_http=401)
    finally:
        remaining = []
        for user in started:
            try:
                if action("recycle", user)["state"] != "recycled":
                    remaining.append(user)
            except (RuntimeError, subprocess.SubprocessError):
                remaining.append(user)
        m1.check(not remaining, f"Restored workspaces could not finish recycling: {remaining}")
    return {"image": image, "environment": ENVIRONMENT, "users": result, "scratch_instances_recycled": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--restored", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    start = datetime.now(timezone.utc).isoformat()
    if args.output.exists():
        parser.error("Choose a new evidence file")
    report = {"start": start, **verify(args.source.resolve(), args.restored.resolve()), "status": "passed",
              "end": datetime.now(timezone.utc).isoformat()}
    with args.output.open("x") as evidence:
        json.dump(report, evidence, indent=2)
        evidence.write("\n")


if __name__ == "__main__":
    main()

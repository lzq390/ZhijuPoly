"""Exercise the M1 development manager and real per-user OpenScience APIs with synthetic data."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import select
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
MANAGER = ROOT / "scripts/openscience_per_user_dev.py"
PROJECT = "same-project"
PROJECTS = "/openscience/files/projects"
GENERAL = "/openscience/files/general"
DIRECTORY = f"{PROJECTS}/{PROJECT}"
FILE = "notebook.txt"
MODEL_REPLY = "M0-SYNTHETIC-OK"

# This client runs in the assigned user's disposable model sidecar. No ports are published.
CLIENT = """import json, sys
import urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urlencode

r = json.load(sys.stdin)
url = "http://127.0.0.1:4096" + r["path"]
if r["query"]:
    url += "?" + urlencode(r["query"])
body = None if r["body"] is None else json.dumps(r["body"]).encode()
token = Path("/run/nexpoly-m1/token").read_text().strip()
req = urllib.request.Request(url, body, {"Content-Type":"application/json", "Authorization":"Bearer " + token},
                             method=r["method"])
try:
    with urllib.request.urlopen(req, timeout=60) as response:
        print(json.dumps({"code":response.status, "body":None if response.status == 204 else json.load(response)}))
except urllib.error.HTTPError as e:
    print(json.dumps({"code": e.code, "error": e.read(256).decode(errors="replace")}))
"""


def run(*args, stdin=None, timeout=120):
    result = subprocess.run(args, input=stdin, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{Path(str(args[0])).name} failed ({result.returncode}): "
                           f"{(result.stderr or result.stdout)[-750:].strip()}")
    return result.stdout.strip()


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def together(items, action):
    with ThreadPoolExecutor(max_workers=len(items)) as pool:
        futures = {key: pool.submit(action, key) for key in items}
        return {key: future.result() for key, future in futures.items()}


def request(binding, endpoint, method="GET", body=None, *, expected_status=None, **query):
    payload = {"path": endpoint, "method": method, "body": body, "query": query}
    result = json.loads(run("docker", "exec", "-i", binding["fixture"], "python", "-B", "-c", CLIENT,
                            stdin=json.dumps(payload), timeout=80))
    if expected_status is not None:
        check(result["code"] == expected_status, f"{method} {endpoint}: expected {expected_status}, got {result['code']}")
        return result["code"]
    if result["code"] not in (200, 201, 204):
        raise RuntimeError(f"{method} {endpoint} returned {result['code']}: {result.get('error','')}")
    return result["body"]


def marker(actor, kind):
    return f"M1 synthetic {actor} {kind}"


def sessions(binding, directory):
    return request(binding, "/session", directory=directory)


def seed(binding, actor):
    created = request(binding, "/api/resolve-folder/create", "POST", {"parent": PROJECTS, "name": PROJECT})
    check(created["absolute"] == DIRECTORY and created["ok"], "project creation returned unexpected directory")
    content = marker(actor, "project file")
    request(binding, "/file/content", "PUT", {"path": FILE, "content": content}, directory=DIRECTORY)
    ids = {}
    for kind, directory in (("general", GENERAL), ("project", DIRECTORY)):
        session = request(binding, "/session", "POST", {"title": marker(actor, kind)}, directory=directory)
        parts = [{"type": "text", "text": marker(actor, kind + " prompt")}]
        if kind == "project":
            parts.append({"type": "file", "mime": "text/plain", "filename": FILE, "url": f"file://{DIRECTORY}/{FILE}"})
        reply = request(binding, f"/session/{session['id']}/message", "POST",
                        {"model": {"providerID": "m0fixture", "modelID": "m0"}, "parts": parts}, directory=directory)
        check(any(p.get("text") == MODEL_REPLY for p in reply["parts"]), "synthetic assistant reply missing")
        ids[kind] = session["id"]
    return ids


def inspect_data(binding, actor, ids, foreign_actor):
    listed = request(binding, "/api/resolve-folder/list")
    check([p["name"] for p in listed["entries"]] == [PROJECT], "unexpected project list")
    validated = request(binding, "/api/resolve-folder/validate", "POST", {"path": DIRECTORY})
    check(validated["ok"] and validated["absolute"] == DIRECTORY, "cannot reopen named project")
    files = request(binding, "/file", directory=DIRECTORY, path="")
    check(any(f.get("name") == FILE for f in files), "expected file missing from listing")
    content = request(binding, "/file/content", directory=DIRECTORY, path=FILE)
    check(content["content"] == marker(actor, "project file"), "same-name file contains incorrect user data")
    digest = hashlib.sha256(content["content"].encode()).hexdigest()
    result = {}
    transcripts = {}
    for kind, directory in (("general", GENERAL), ("project", DIRECTORY)):
        listed = sessions(binding, directory)
        check([(s["id"], s["title"]) for s in listed] == [(ids[kind], marker(actor, kind))],
              f"{actor}/{kind} session listing differs from owner's expected records")
        saved = request(binding, f"/session/{ids[kind]}/message", directory=directory)
        check(marker(foreign_actor, "") not in json.dumps(saved, ensure_ascii=False), "unexpected foreign data in messages")
        check(any(m["info"]["role"] == "user" and any(p.get("text") == marker(actor, kind + " prompt")
                                                      for p in m["parts"]) for m in saved), "owner prompt missing")
        check(any(m["info"]["role"] == "assistant" and any(p.get("text") == MODEL_REPLY for p in m["parts"])
                  for m in saved), "stored assistant response missing")
        if kind == "project":
            check(any(p["type"] == "file" and p.get("filename") == FILE for m in saved for p in m["parts"]),
                  "project attachment missing from stored message")
        result[kind] = ids[kind]
        transcripts[kind] = transcript_digest(saved)
    return {"session_ids": result, "transcript_sha256": transcripts, "file_sha256": digest,
            "project_attachment": FILE, "foreign_markers": 0}


def transcript_digest(messages):
    serialized = json.dumps(messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode()).hexdigest()


def inspect_runtime(binding, deployment, expected_image):
    name = binding["container"]
    facts = json.loads(run("docker", "inspect", "--format",
                           '{"uid":{{json .Config.User}},"volumes":{{json .Mounts}},"id":{{json .Id}},'
                           '"started_at":{{json .State.StartedAt}},"image":{{json .Image}},"ports":{{json .HostConfig.PortBindings}},'
                           '"network":{{json .HostConfig.NetworkMode}},"deployment":'
                           '{{json (index .Config.Labels "org.nexpoly.m1.deployment")}}}', name))
    volumes = [mount for mount in facts["volumes"] if mount["Type"] == "volume"]
    private = [m for m in volumes if m["Destination"] == "/openscience" and m["RW"]]
    check(len(private) == 1 and private[0]["Name"] == binding["volume"], "wrong private data mount")
    check(len([m for m in volumes if m["RW"]]) == 1, "additional writable volume mounted")
    check(not facts["ports"] and facts["network"].startswith("container:"), "backend exposed outside paired network")
    fixture = json.loads(run("docker", "inspect", "--format",
                             '{"id":{{json .Id}},"network":{{json .HostConfig.NetworkMode}}}', binding["fixture"]))
    check(facts["network"][len("container:"):] in (fixture["id"], binding["fixture"]) and fixture["network"] == "none",
          "invalid private loopback pairing")
    check(facts["id"] == binding["container_id"] and facts["image"] == expected_image
          and facts["deployment"] == deployment, "manager/container metadata differ")
    check(facts["uid"] == f"{binding['uid']}:{binding['gid']}", "wrong runtime uid/gid")
    # Only public binding identifiers enter the evidence; no mount source paths or config/credentials.
    owned = json.loads(run("docker", "exec", name, "bun", "-e",
                           'const fs=require("fs"); const s=fs.statSync("/openscience"); '
                           'console.log(JSON.stringify([s.uid,s.gid,s.mode & 511]))'))
    check(owned == [binding["uid"], binding["gid"], 0o700], "private volume root must be owned by instance at mode 0700")
    fields = ("environment", "user_id", "uid", "gid", "volume", "container_id", "generation")
    return {**{k: binding[k] for k in fields}, "image": facts["image"], "started_at": facts["started_at"], "volume_root_mode": "0700"}


def compare_binding(before, after, restarted=False, recreated=False):
    for key in ("environment", "user_id", "uid", "gid", "volume"):
        check(before[key] == after[key], f"unstable {key}")
    if restarted:
        check(before["container_id"] == after["container_id"], "restart unexpectedly replaced container")
        check(before["started_at"] != after["started_at"], "restart did not start a new backend process")
    if recreated:
        check(before["container_id"] != after["container_id"] and after["generation"] > before["generation"],
              "recycled instance was not replaced by a fresh generation")


def busy_project(binding, sid, recycle):
    """Hold the synthetic model for one real project request. The backend remains scheduled."""
    fixture = binding["fixture"]
    run("docker", "kill", "--signal=STOP", fixture)
    try:
        request(binding, f"/session/{sid}/prompt_async", "POST",
                {"model": {"providerID": "m0fixture", "modelID": "m0"},
                 "parts": [{"type": "text", "text": "M1 synthetic lifecycle follow-up"}]}, directory=DIRECTORY)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            statuses = request(binding, "/session/status", directory=DIRECTORY)
            if statuses.get(sid, {}).get("type") == "busy":
                break
            time.sleep(.15)
        else:
            raise RuntimeError("project never reported a busy session")
        check(all(s["type"] == "idle" for s in request(binding, "/session/status", directory=GENERAL).values()),
              "expected general workspace to be idle while only the project is active")
        retained = recycle()
        check(retained["state"] == "running" and retained["container_id"] == binding["container_id"],
              "manager recycled a backend with an active project round")
        check(retained["reason"] in ("active_session", "busy"), f"unexpected active project reason: {retained['reason']}")
    finally:
        run("docker", "kill", "--signal=CONT", fixture)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        saved = request(binding, f"/session/{sid}/message", directory=DIRECTORY)
        replies = [m for m in saved if m["info"]["role"] == "assistant" and
                   any(p.get("text") == MODEL_REPLY for p in m["parts"])]
        if len(replies) == 2 and request(binding, "/session/status", directory=DIRECTORY).get(sid, {}).get("type", "idle") == "idle":
            return {"general_idle": True, "project_status": "busy", "manager_reason": retained["reason"],
                    "completed_project_replies": len(replies), "transcript_sha256": transcript_digest(saved)}
        time.sleep(.2)
    raise RuntimeError("waiting project round did not finish after the model resumed")


def passive_stream(binding):
    """Wait for the real SSE connection before the idle check. The stream sends no business request."""
    code = """import sys, urllib.request
from pathlib import Path
token = Path("/run/nexpoly-m1/token").read_text().strip()
req = urllib.request.Request("http://127.0.0.1:4096/event", headers={"Authorization":"Bearer "+token})
with urllib.request.urlopen(req,timeout=30) as r:
    assert r.status == 200 and "text/event-stream" in r.headers["Content-Type"]
    assert r.readline().strip()
    print("connected",flush=True)
    r.read()
"""
    proc = subprocess.Popen(["docker", "exec", binding["fixture"], "python", "-B", "-c", code],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        if not select.select([proc.stdout], [], [], 12)[0] or proc.stdout.readline().strip() != "connected":
            raise RuntimeError("passive SSE client did not connect")
        return proc
    except BaseException:
        proc.terminate()
        proc.wait(timeout=10)
        raise


def generated_resources(deployment):
    selector = "label=org.nexpoly.m1.deployment=" + deployment
    return {
        "containers": run("docker", "ps", "-aq", "--no-trunc", "--filter", selector).split(),
        "volumes": run("docker", "volume", "ls", "-q", "--filter", selector).split(),
    }


def evidence(image, restore_image, state_dir, report):
    def manager(*args):
        return json.loads(run(sys.executable, str(MANAGER), "--state-dir", str(state_dir), *args))

    run_id = uuid4().hex[:12]
    primary = "dev-m1-" + run_id
    alternate = "dev-alt-" + run_id
    users = {"a": "test-user-a", "b": "test-user-b"}
    report.update({"image": image, "environment": primary, "alternate_environment": alternate, "stages": {}})
    stages = report["stages"]
    initialized = manager("init", "--image", image, "--idle-seconds", "2", "--max-active", "2")
    deployment = initialized["deployment"]
    report["deployment"] = deployment

    def act(verb, role, *args, environment=primary):
        return manager(verb, "--environment", environment, "--user-id", users[role], *args)

    def inspect(bindings, expected_image):
        return together(users, lambda r: inspect_runtime(bindings[r], deployment, expected_image))

    def snapshot(bindings, ids):
        def verify(role):
            other = "b" if role == "a" else "a"
            data = inspect_data(bindings[role], users[role], ids[role], users[other])
            data["other_project_session_status"] = request(bindings[role], f"/session/{ids[other]['project']}",
                                                           expected_status=404, directory=DIRECTORY)
            return data
        return together(users, verify)

    try:
        same_user = together((1, 2), lambda _: act("ensure", "a"))
        check(same_user[1]["container_id"] == same_user[2]["container_id"], "duplicate instance on concurrent ensure")
        bindings = {"a": same_user[1], "b": act("ensure", "b")}
        initial = inspect(bindings, image)
        for field in ("uid", "gid", "volume", "container_id"):
            check(initial["a"][field] != initial["b"][field], f"two users share {field}")
        check(bindings["a"]["fixture"] != bindings["b"]["fixture"], "two users share loopback model")
        ids = together(users, lambda r: seed(bindings[r], users[r]))
        check(set(ids["a"].values()).isdisjoint(ids["b"].values()), "unexpected cross-user session IDs")
        try:
            act("ensure", "a", environment=alternate)
        except RuntimeError as exc:
            check(any(text in str(exc).lower() for text in ("capacity", "limit", "maximum")),
                  f"third instance failed for an unexpected reason: {exc}")
        else:
            raise AssertionError("capacity limit did not retain the two active workspace reservations")
        if restore_image:
            try:
                manager("set-image", "--image", restore_image)
            except RuntimeError as exc:
                check("template_in_use" in str(exc), "active-template change returned unexpected refusal")
            else:
                raise AssertionError("active deployment allowed an incompatible template change")
        stages["created"] = {"bindings": initial, "owner_reads": snapshot(bindings, ids), "concurrent_ensure_idempotent": True, "third_active_rejected": True}
        print("Two users created same-name projects/files, with independent general and project sessions.", flush=True)

        bindings = together(users, lambda r: act("restart", r))
        restarted = inspect(bindings, image)
        for role in users:
            compare_binding(initial[role], restarted[role], restarted=True)
        after_restart = snapshot(bindings, ids)
        check(after_restart == stages["created"]["owner_reads"], "persisted transcripts or files changed after restart")
        stages["restarted"] = {"bindings": restarted, "owner_reads": after_restart}
        print("Both restarted backends restored the correct files, attachments and conversations.", flush=True)

        stages["busy_project"] = busy_project(bindings["a"], ids["a"]["project"], lambda: act("recycle", "a"))
        ticket = act("begin", "a")
        try:
            busy = act("recycle", "a")
            check(busy["state"] == "running" and busy["reason"] == "busy", "active business lease was ignored")
        finally:
            act("end", "a", "--lease", ticket["lease"])
        stream = passive_stream(bindings["a"])
        try:
            time.sleep(2.1)
            check(stream.poll() is None, "SSE disconnected before checking idle eviction")
            act("reap", "a")
        finally:
            if stream.poll() is None:
                stream.terminate()
            stream.wait(timeout=15)
        act("recycle", "b")
        survivors = generated_resources(deployment)
        check(not survivors["containers"], "recycled users still have containers")
        check({initial[r]["volume"] for r in users}.issubset(set(survivors["volumes"])), "recycle lost a private volume")
        stages["recycled"] = {"remaining_containers": 0, "private_volumes_preserved": 2,
                              "active_lease_retained": True, "idle_passive_sse_reaped": True}
        print("An active lease blocked recycling; an idle instance with a passive SSE client was reaped.", flush=True)

        alternate_binding = act("ensure", "a", environment=alternate)
        alternate_facts = inspect_runtime(alternate_binding, deployment, image)
        for field in ("uid", "volume", "container_id"):
            check(all(alternate_facts[field] != initial[r][field] for r in users), f"alternate environment reused {field}")
        check(request(alternate_binding, "/api/resolve-folder/list")["entries"] == [], "alternate environment saw projects")
        check(sessions(alternate_binding, GENERAL) == [], "alternate environment saw general sessions")
        act("recycle", "a", environment=alternate)
        stages["second_environment"] = {"binding": alternate_facts, "projects": 0, "general_sessions": 0}

        if restore_image:
            check(image != restore_image, "test upgrade requires two distinct image IDs")
            manager("set-image", "--image", restore_image)
            stages["template_change"] = {"initial_image": image, "restored_image": restore_image, "denied_with_active_instances": True}
        bindings = together(users, lambda r: act("ensure", r))
        restored = inspect(bindings, restore_image or image)
        for role in users:
            compare_binding(initial[role], restored[role], recreated=True)
        after_recreate = snapshot(bindings, ids)
        expected = json.loads(json.dumps(after_restart))
        expected["a"]["transcript_sha256"]["project"] = stages["busy_project"]["transcript_sha256"]
        check(after_recreate == expected, "full transcripts or private files changed after volume reattachment")
        stages["recreated"] = {"bindings": restored, "owner_reads": after_recreate}
        print("Fresh container generations reopened both private volumes with the original owner data.", flush=True)

        busy_final = busy_project(bindings["b"], ids["b"]["project"], lambda: act("recycle", "b"))
        expected["b"]["transcript_sha256"]["project"] = busy_final["transcript_sha256"]
        stages["restored_busy_project"] = busy_final
        # The immutable ID has just been checked against this test deployment, image and private mount.
        run("docker", "kill", "--signal=KILL", restored["a"]["container_id"])
        exited = act("status", "a")
        check(exited["state"] == "exited", "simulated process failure did not stop the test backend")
        unaffected = inspect_data(bindings["b"], users["b"], ids["b"], users["a"])
        unaffected["other_project_session_status"] = request(bindings["b"], f"/session/{ids['a']['project']}",
                                                              expected_status=404, directory=DIRECTORY)
        check(unaffected == expected["b"], "other user lost data while the failed process was down")
        bindings["a"] = act("ensure", "a")
        recovered = inspect(bindings, restore_image or image)
        compare_binding(restored["a"], recovered["a"], recreated=True)
        check(restored["b"] == recovered["b"], "recovering one user restarted the unaffected backend")
        final_data = snapshot(bindings, ids)
        check(final_data == expected, "crash recovery lost or misassigned saved data")
        stages["process_failure_recovery"] = {"bindings": recovered, "owner_reads": final_data,
                                              "other_user_read_during_failure": True}
        print("A process exit was recovered; B stayed readable, with full transcripts preserved.", flush=True)
    finally:
        # Only this fresh state directory's generated deployment is eligible for deletion.
        owned = generated_resources(deployment)
        if owned["containers"]:
            run("docker", "rm", "-f", *owned["containers"])
        if owned["volumes"]:
            run("docker", "volume", "rm", *owned["volumes"])
        report["remaining"] = generated_resources(deployment)
        check(not any(report["remaining"].values()), "test resources remain after cleanup")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Full local M1 candidate image ID")
    parser.add_argument("--restore-image", help="Distinct, locally approved template used for restored instances")
    parser.add_argument("--output", required=True, type=Path, help="New JSON result file")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        def interrupt(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupt)
        report = {"start": datetime.now(timezone.utc).isoformat(), "status": "failed"}
        try:
            with TemporaryDirectory(prefix="nexpoly-m1-verify-") as state_dir:
                evidence(args.image, args.restore_image, Path(state_dir), report)
            report["status"] = "passed"
        except (Exception, KeyboardInterrupt) as exc:
            report["error"] = str(exc) or type(exc).__name__
            print(report["error"], file=sys.stderr)
        finally:
            report["end"] = datetime.now(timezone.utc).isoformat()
            json.dump(report, output, indent=2)
            output.write("\n")
    print(f"{report['status']}: {args.output}")
    return int(report["status"] != "passed")


if __name__ == "__main__":
    sys.exit(main())

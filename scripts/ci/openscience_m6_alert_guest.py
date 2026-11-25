"""Offline M6 alert guest. Inputs and all target block devices are created by the host fixture."""

import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import shutil
import socketserver
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

OPS = Path("/opt/nexpoly/ops/openscience-per-user-dev")
SOCKET = Path("/run/m6-operator-alerts.sock")
DATABASE = Path("/backup/operator-events.sqlite")
SERVICE = "m5-disk-alert-relay.service"


def stage():
    inputs = Path("/inputs")
    spec = json.loads((inputs / "spec.json").read_bytes())
    assert spec["fixture"] == "openscience-m6-alert-drill-v1"
    assert (
        subprocess.check_output(
            ["findmnt", "-nro", "FSTYPE", "--mountpoint", inputs], text=True
        ).strip()
        == "iso9660"
    )
    OPS.mkdir(parents=True, exist_ok=False)
    scripts = OPS.parents[1] / "scripts"
    scripts.mkdir()
    for name in ("water_monitor", "water_pool", "water_alerts", "quota", "quota_nfs"):
        shutil.copyfile(inputs / (name + ".py"), OPS / (name + ".py"))
    shutil.copyfile(
        inputs / "openscience_per_user_dev.py", scripts / "openscience_per_user_dev.py"
    )


if __name__ == "__main__" and not sys.argv[1:]:
    stage()
sys.path[:0] = [str(OPS), str(OPS.parents[1] / "scripts")]
import water_monitor as monitor  # noqa: E402
import water_pool as pool  # noqa: E402
import quota  # noqa: E402
import quota_nfs  # noqa: E402
from openscience_per_user_dev import write_atomic  # noqa: E402


def receiver():
    SOCKET.unlink(missing_ok=True)
    db = sqlite3.connect(DATABASE)
    with db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, body BLOB NOT NULL)"
        )
        db.execute("CREATE TABLE IF NOT EXISTS attempts(id TEXT NOT NULL)")

    class Inbox(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            assert self.path == "/watermark" and 0 < length <= 16384
            raw = self.rfile.read(length)
            event = json.loads(raw)
            key = self.headers["Idempotency-Key"]
            assert key == event["id"]
            with db:
                db.execute("INSERT INTO attempts VALUES (?)", (key,))
                old = db.execute(
                    "SELECT body FROM events WHERE id=?", (key,)
                ).fetchone()
                if old:
                    assert old[0] == raw
                else:
                    db.execute("INSERT INTO events VALUES (?,?)", (key, raw))
            # Persist the first incident, then deliberately lose the reply and stop the receiver.
            if event["sequence"] == 1 and not old:
                os._exit(75)
            body = json.dumps({"id": key}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    with socketserver.UnixStreamServer(str(SOCKET), Inbox) as server:
        server.serve_forever()


def command(*args):
    return subprocess.check_output([str(a) for a in args], text=True).strip()


def wait_for(fn, label, timeout=45):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.3)
    raise TimeoutError(label)


def setup():
    node = pool.pool_device()
    assert subprocess.run(["blkid", "-p", node], capture_output=True).returncode == 2
    command("mkfs.ext4", "-q", "-F", "-m", "0", node)
    pool_uuid = command("blkid", "-s", "UUID", "-o", "value", node)
    pool.POOL.mkdir()
    command("mount", "-o", "nodev,nosuid,noexec", node, pool.POOL)
    users = json.loads(Path("/inputs/spec.json").read_bytes())["users"]
    pins = {}
    for role, user in users.items():
        image = pool.POOL / ("src-" + role + ".raw")
        with image.open("xb") as output:
            output.truncate(3 * 1024**3)
        command(
            "mkfs.ext4",
            "-F",
            "-q",
            "-m",
            "0",
            "-O",
            "quota",
            "-E",
            "quotatype=usrquota:grpquota",
            "-L",
            "m5quota-src-" + role,
            image,
        )
        root = pool.ROOTS / ("src-" + role)
        root.mkdir(parents=True)
        command(
            "mount",
            "-o",
            "loop,usrquota,grpquota,discard,nodev,nosuid,noexec",
            image,
            root,
        )
        installed = quota.install(root, user)
        pins[role] = {k: user[k] for k in ("workspace", "uid")}
        pins[role]["filesystem_uuid"] = installed["filesystem_uuid"]
    pool.install_config(
        dict(
            format="nexpoly-dev-m1-waterpool-v1",
            pool_bytes=pool.POOL_BYTES,
            filesystem_uuid=pool_uuid,
            sources=pins,
        )
    )
    mounts = {"m5-" + k: row["filesystem_uuid"] for k, row in pins.items()}
    write_atomic(Path("/etc/nexpoly-m5-disks.json"), json.dumps(mounts).encode(), 0o600)
    quota_nfs.CLAIMS.parent.mkdir()
    claim = quota_nfs.pinned_guest_claim(
        "src", uuid4().hex, hashlib.sha256(os.urandom(16)).hexdigest(), acquire=True
    )
    # The fixture has already mounted the new disks. Let the real monitor depend on their live attestation.
    Path("/etc/systemd/system/m5-water-pool.service").write_text(
        "[Unit]\nDescription=Verify freshly mounted drill pool\n[Service]\nType=oneshot\nRemainAfterExit=yes\n"
        f"ExecStart=/usr/bin/python3 -IB {OPS}/water_pool.py sample\n"
    )
    args = (
        claim["deployment"],
        claim["registry_sha256"],
        pool_uuid,
        96 * pool.MIB,
        160 * pool.MIB,
    )
    return args, pins


def drill():
    args, sources = setup()

    def monitor_call(action, *options):
        return json.loads(
            command(
                "/usr/bin/python3",
                "-IB",
                OPS / "water_monitor.py",
                action,
                *args,
                *options,
            )
        )

    def state():
        return json.loads((monitor.ROOT / "state.json").read_bytes())

    def cursor():
        return json.loads((monitor.ROOT / "receiver.json").read_bytes())["last_receipt"]

    normal = monitor_call("configure", "--alert-socket", SOCKET)
    assert (
        normal["state"] == "normal"
        and command("systemctl", "is-active", SERVICE) == "active"
    )
    # Reapply the actual manager's guest command against an already running producer.
    assert monitor_call("configure", "--alert-socket", SOCKET)["sequence"] == 0
    pool.pressure(64)
    wait_for(lambda: state()["state"] == "low", "monitor did not enter low")
    low = state()
    lookup = ("journalctl", "-b", "-u", SERVICE, "--no-pager", "-o", "cat")
    wait_for(
        lambda: "FileNotFoundError" in command(*lookup),
        "relay did not attempt to contact absent receiver",
    )
    assert cursor() is None
    pool.pressure()
    wait_for(
        lambda: state()["state"] == "normal",
        "monitor did not recover during receiver outage",
    )
    clear = state()
    assert low["sequence"] == 1 and clear["sequence"] == 2 and cursor() is None
    for snapshot, mode in ((low, "block"), (clear, "open")):
        assert all(w["group_gate"] == mode for w in snapshot["workspaces"].values())
    proc = subprocess.run([sys.executable, __file__, "receiver"], timeout=45)
    assert proc.returncode == 75 and cursor() is None
    with sqlite3.connect(DATABASE) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone() == (1,)
    command("systemctl", "restart", SERVICE)
    with subprocess.Popen([sys.executable, __file__, "receiver"]) as proc:
        try:
            wait_for(
                lambda: cursor() and cursor()["sequence"] == 2,
                "restart did not replay and match both receipts",
            )
            command("systemctl", "restart", SERVICE)
            time.sleep(monitor.CADENCE_SECONDS + 1)
            events = monitor_call("alerts")
            with sqlite3.connect(DATABASE) as db:
                received = [
                    json.loads(body)
                    for (body,) in db.execute("SELECT body FROM events ORDER BY rowid")
                ]
                attempts = [
                    key
                    for (key,) in db.execute("SELECT id FROM attempts ORDER BY rowid")
                ]
            assert (
                received
                == [low["last_transition"], clear["last_transition"]]
                == events["alerts"]
            )
            assert events["pending"] == 0
            first, second = [event["id"] for event in received]
            assert attempts == [first, first, second] and cursor()["id"] == second
            return dict(
                status="passed",
                pool_bytes=pool.POOL_BYTES,
                filesystem_uuid=normal["filesystem_uuid"],
                initial_available_bytes=normal["available_bytes"],
                workspaces=sources,
                source_service=monitor.SERVICE,
                relay_service=SERVICE,
                receiver="guest-local independent SQLite inbox",
                receiver_absent_on_firing=True,
                recovered_while_receiver_down=True,
                first_ack_lost_after_receiver_commit=True,
                receiver_restarted=True,
                relay_restarts=2,
                events=received,
                attempts=attempts,
                cursor=cursor(),
                kernel_group_gates={"firing": "both blocked", "resolved": "both open"},
            )
        finally:
            proc.terminate()


if __name__ == "__main__":
    if sys.argv[1:] == ["receiver"]:
        receiver()
    else:
        print("M5_QUOTA_RESULT=" + json.dumps(drill(), sort_keys=True), flush=True)

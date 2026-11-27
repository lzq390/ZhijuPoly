"""Receive and look up deploy-m6 disk alerts in the host operator's durable inbox."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import http.client
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socket
import socketserver
import sqlite3
import stat
import sys
from urllib.parse import quote, unquote
from uuid import UUID, uuid4

CANDIDATE = Path.home() / ".local/state/nexpoly-m6-deploy-01"
SOCKET = f"/run/user/{os.getuid()}/nexpoly-deploy-m6-oncall.sock"
ROUTE = "/receipts/"
MAX_EVENT = 16384


def inbox_path(root):
    directory = root / "oncall"
    info = directory.lstat()
    if (
        info.st_uid != os.getuid()
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("Expected a private deploy-m6 oncall directory (0700)")
    db = directory / "receipts.sqlite3"
    try:
        info = db.lstat()
    except FileNotFoundError:
        return db
    if (
        info.st_uid != os.getuid()
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("Expected a private regular receipt database (0600)")
    return db


def initialize(root):
    registry = json.loads((root / "ws/registry.json").read_bytes())
    deploy = registry["deployment"]
    fs = registry["disk_watermark"]["filesystem_uuid"]
    owners = registry["candidate_owners"]
    claim = registry["native_claim"]
    if (
        registry["format"] != 5
        or set(owners) != {"a", "b"}
        or any(row["environment"] != "deploy-m6" for row in owners.values())
        or claim["phase"] != "src"
        or claim["deployment"] != deploy
        or UUID(deploy).hex != deploy
        or str(UUID(fs)) != fs
    ):
        raise ValueError(
            "Expected the enrolled deploy-m6 source deployment and target disk"
        )
    database = inbox_path(root)
    fd = os.open(database, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS identity (slot INTEGER PRIMARY KEY CHECK(slot=1), "
            "deployment TEXT NOT NULL, filesystem_uuid TEXT NOT NULL)"
        )
        db.execute("INSERT OR IGNORE INTO identity VALUES (1,?,?)", (deploy, fs))
        if db.execute(
            "SELECT deployment,filesystem_uuid FROM identity WHERE slot=1"
        ).fetchone() != (deploy, fs):
            raise ValueError("Inbox already belongs to another deployment or disk")
        db.execute(
            "CREATE TABLE IF NOT EXISTS receipts (event_id TEXT PRIMARY KEY, receipt_id TEXT UNIQUE NOT NULL, "
            "received_at TEXT NOT NULL, event TEXT NOT NULL)"
        )
    return {
        "environment": "deploy-m6",
        "deployment": deploy,
        "filesystem_uuid": fs,
        "database": str(database),
    }


class Conflict(ValueError):
    pass


def receive(db, identity, key, event):
    if not isinstance(event, dict):
        raise ValueError("Expected a transition")
    deployment, fs = identity
    seq = event.get("sequence")
    notifications = {"low": "firing", "normal": "resolved", "unavailable": "degraded"}
    if (
        type(seq) is not int
        or seq < 1
        or event.get("environment") != "deploy-m6"
        or event.get("deployment") != deployment
        or event.get("filesystem_uuid") != fs
        or key != event.get("id")
        or key != f"{deployment}:{fs}:{seq}"
        or event.get("state") not in notifications
        or event.get("notification") != notifications[event["state"]]
    ):
        raise ValueError("Event differs from the bound target or its idempotency key")
    body = json.dumps(event, allow_nan=False, sort_keys=True, separators=(",", ":"))
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT event FROM receipts WHERE event_id=?", (key,)
        ).fetchone()
        if old:
            if old[0] != body:
                raise Conflict("Event ID has different content")
            return
        receipt_id = uuid4().hex
        received = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO receipts VALUES (?,?,?,?)", (key, receipt_id, received, body)
        )
    print(
        json.dumps(
            {
                "environment": "deploy-m6",
                "event_id": key,
                "notification": event["notification"],
                "receipt_id": receipt_id,
                "received_at": received,
            }
        ),
        flush=True,
    )


def receipt(row):
    return {"receipt_id": row[0], "received_at": row[1], "event": json.loads(row[2])}


class Inbox(BaseHTTPRequestHandler):
    timeout = 3

    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/watermark":
            return self.reply(404, {"error": "unknown_route"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_EVENT or self.headers.get("Transfer-Encoding"):
                return self.reply(400, {"error": "invalid_event_size"})
            body = self.rfile.read(length)
            if len(body) != length:
                return self.reply(400, {"error": "incomplete_event"})
            key = self.headers.get("Idempotency-Key")
            receive(self.server.db, self.server.identity, key, json.loads(body))
        except Conflict:
            return self.reply(409, {"error": "event_content_conflict"})
        except (TypeError, ValueError, RecursionError):
            return self.reply(400, {"error": "invalid_event"})
        except sqlite3.Error:
            return self.reply(503, {"error": "receipt_store_unavailable"})
        self.reply(200, {"id": key})

    def do_GET(self):
        fields = "SELECT receipt_id,received_at,event FROM receipts"
        if self.path == "/receipts":
            rows = self.server.db.execute(fields + " ORDER BY rowid DESC LIMIT 32")
            return self.reply(200, {"receipts": [receipt(row) for row in rows]})
        if self.path.startswith(ROUTE):
            row = self.server.db.execute(
                fields + " WHERE event_id=?", (unquote(self.path[len(ROUTE) :]),)
            ).fetchone()
            if row:
                return self.reply(200, receipt(row))
        self.reply(404, {"error": "receipt_not_found"})

    def log_message(self, *_):
        pass  # New committed receipts are logged once by receive().


def serve(root):
    if (
        os.environ.get("LISTEN_PID") != str(os.getpid())
        or os.environ.get("LISTEN_FDS") != "1"
    ):
        raise ValueError("Run through the deploy-m6 systemd socket unit")
    with socket.socket(fileno=3) as listener:
        if (
            listener.family != socket.AF_UNIX
            or listener.getsockname() != SOCKET
            or not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
        ):
            raise ValueError("Unexpected oncall listener")
        database = inbox_path(root)
        with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as db:
            db.execute("PRAGMA synchronous=FULL")
            identity = db.execute(
                "SELECT deployment,filesystem_uuid FROM identity WHERE slot=1"
            ).fetchone()
            if identity is None:
                raise ValueError("Initialize receiver identity before accepting alerts")
            # systemd owns the socket name across service restarts.
            with socketserver.UnixStreamServer(
                SOCKET, Inbox, bind_and_activate=False
            ) as server:
                server.socket.close()
                server.socket = listener
                server.db = db
                server.identity = identity
                server.serve_forever()


def lookup(event_id):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(5)
        sock.connect(SOCKET)
        conn = http.client.HTTPConnection("localhost")
        conn.sock = sock
        try:
            path = (
                ROUTE + quote(event_id, safe="")
                if event_id is not None
                else "/receipts"
            )
            conn.request("GET", path)
            with conn.getresponse() as response:
                data = json.load(response)
                if response.status != 200:
                    raise ValueError(data["error"])
                return data
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    tasks = parser.add_subparsers(dest="action", required=True)
    for name in ("init", "serve", "latest"):
        tasks.add_parser(name)
    tasks.add_parser("query").add_argument("event_id")
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "serve":
        serve(CANDIDATE)
    else:
        value = (
            initialize(CANDIDATE)
            if args.action == "init"
            else lookup(getattr(args, "event_id", None))
        )
        print(json.dumps(value))


if __name__ == "__main__":
    try:
        main()
    except (sqlite3.Error, ValueError, OSError) as exc:
        sys.exit(str(exc))

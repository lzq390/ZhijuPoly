"""Exercise durable operator readbacks and the relay's actual Unix HTTP protocol."""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import socketserver
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

OPS = Path(__file__).resolve().parents[2] / "ops/openscience-per-user-dev"
sys.path.insert(0, str(OPS))
import oncall_receiver as inbox  # noqa: E402
import water_alerts  # noqa: E402


class OperatorReceipts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "oncall").mkdir(mode=0o700)
        (self.root / "ws").mkdir()
        self.fs = str(uuid4())
        self.deploy = uuid4().hex
        self.registry = {
            "format": 5,
            "deployment": self.deploy,
            "disk_watermark": {"filesystem_uuid": self.fs},
            "native_claim": {"phase": "src", "deployment": self.deploy},
            "candidate_owners": {r: {"environment": "deploy-m6"} for r in "ab"},
        }
        self.config = self.root / "ws/registry.json"
        self.config.write_text(json.dumps(self.registry))
        self.database = Path(inbox.initialize(self.root)["database"])
        self.sock = str(self.root / "inbox.sock")

    def event(self, seq, state):
        return {
            "id": water_alerts.event_id(self.fs, seq, self.deploy),
            "deployment": self.deploy,
            "filesystem_uuid": self.fs,
            "environment": "deploy-m6",
            "sequence": seq,
            "state": state,
            "notification": {"low": "firing", "normal": "resolved"}[state],
        }

    @contextmanager
    def service(self):
        with closing(sqlite3.connect(self.database, check_same_thread=False)) as db:
            with socketserver.UnixStreamServer(self.sock, inbox.Inbox) as server:
                server.identity = db.execute(
                    "SELECT deployment,filesystem_uuid FROM identity"
                ).fetchone()
                server.db = db
                thread = threading.Thread(
                    target=server.serve_forever, kwargs={"poll_interval": 0.01}
                )
                thread.start()
                try:
                    with patch.object(inbox, "SOCKET", self.sock):
                        yield
                finally:
                    server.shutdown()
                    thread.join(2)
        os.unlink(self.sock)

    def test_committed_readback_and_duplicate_after_restart(self):
        first, second = self.event(3, "low"), self.event(4, "normal")
        with self.service():
            self.assertEqual(inbox.lookup(None), {"receipts": []})
            water_alerts.post(self.sock, first)
            received = inbox.lookup(first["id"])
            self.assertEqual(received["event"], first)
            self.assertTrue(received["received_at"])
            self.assertEqual(len(received["receipt_id"]), 32)
        inbox.initialize(self.root)
        with self.service():
            water_alerts.post(self.sock, dict(reversed(first.items())))
            self.assertEqual(inbox.lookup(first["id"]), received)
            with self.assertRaisesRegex(ValueError, "409"):
                water_alerts.post(self.sock, dict(first, reason="different"))
            water_alerts.post(self.sock, second)
            resolved = inbox.lookup(second["id"])
            self.assertNotEqual(resolved["receipt_id"], received["receipt_id"])
            self.assertEqual(inbox.lookup(None), {"receipts": [resolved, received]})
        self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)

    def test_wrong_target_and_missing_id(self):
        event = self.event(1, "low")
        with self.service():
            for bad in (
                dict(environment="dev-m1"),
                dict(filesystem_uuid=str(uuid4())),
                dict(deployment=uuid4().hex),
                dict(sequence=9),
                dict(notification="resolved"),
            ):
                with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "400"):
                    water_alerts.post(self.sock, dict(event, **bad))
            with self.assertRaisesRegex(ValueError, "receipt_not_found"):
                inbox.lookup(event["id"])
            self.assertEqual(inbox.lookup(None)["receipts"], [])
        self.registry["disk_watermark"]["filesystem_uuid"] = str(uuid4())
        self.config.write_text(json.dumps(self.registry))
        with self.assertRaisesRegex(ValueError, "already belongs"):
            inbox.initialize(self.root)

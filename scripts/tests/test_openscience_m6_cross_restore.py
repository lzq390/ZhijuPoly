"""Verify the two-user transfer and resume it without allocating a second package."""

import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import openscience_m5_quota_bundle as bundle


class CrossHostTransferTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.backup = Path(directory.name)
        self.published = self.backup / "checkpoint"
        prior = os.umask(0o077)
        self.addCleanup(os.umask, prior)
        guest = SimpleNamespace(BACKUP=self.backup, bundle=bundle, MIB=2**20, check=self.check)
        path = SCRIPTS.parent / "ops/openscience-per-user-dev/quota-candidate-import.py"
        with patch.dict(sys.modules, quota_guest=guest), patch.object(sys, "path", list(sys.path)):
            self.receive = runpy.run_path(str(path))["receive"]
        self.members = {}
        self.pins = []
        for role in "ab":
            data = (role + " data").encode()
            manifest = json.dumps({"archive_sha256": hashlib.sha256(data).hexdigest()}).encode()
            self.members[role + "/manifest.json"] = manifest
            self.members[role + "/workspace.tar"] = data
            self.pins.append(hashlib.sha256(manifest).hexdigest())

    @staticmethod
    def check(condition, message):
        if not condition:
            raise ValueError(message)

    def send(self, members, pins):
        transport = io.BytesIO()
        with tarfile.open(fileobj=transport, mode="w") as tar:
            for name, data in members.items():
                item = tarfile.TarInfo(name)
                item.size = len(data)
                tar.addfile(item, io.BytesIO(data))
        transport.seek(0)
        with patch.object(sys, "stdin", SimpleNamespace(buffer=transport)):
            self.receive(self.published, pins)

    def test_retry_verifies_without_allocating_or_replacing_either_archive(self):
        self.send(self.members, self.pins)
        before = {p: p.stat().st_ino for p in self.published.rglob("*")}
        with patch.object(bundle, "require_space", side_effect=AssertionError("allocated a duplicate package")):
            self.send(self.members, self.pins)
            corrupt = {**self.members, "a/workspace.tar": self.members["b/workspace.tar"]}
            with self.assertRaises(ValueError):
                self.send(corrupt, self.pins)
        self.assertEqual({p: p.stat().st_ino for p in self.published.rglob("*")}, before)
        for name, content in self.members.items():
            self.assertEqual((self.published / name).read_bytes(), content)

    def test_incomplete_or_swapped_pair_is_never_published(self):
        with self.assertRaises(ValueError):
            self.send(self.members, self.pins[::-1])
        partial = self.members.copy()
        del partial["b/workspace.tar"]
        with self.assertRaises(ValueError):
            self.send(partial, self.pins)
        self.assertFalse(self.published.exists())

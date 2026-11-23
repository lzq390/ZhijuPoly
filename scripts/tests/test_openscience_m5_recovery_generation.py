"""The offline M5 two-owner browser drill resumes on its sealed generation."""

import errno
import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

scripts = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(scripts), str(scripts / "ci")]
import openscience_m5_guest_lifecycle as lifecycle


class RecoveryGenerationTests(unittest.TestCase):
    def test_retry_after_a_published_and_b_failed(self):
        users = {role: {"environment": "dev-m1", "user_id": "test-user-" + role,
                        "uid": 210000 + i, "workspace": role * 32}
                 for i, role in enumerate(("a", "b"))}
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            directory = base / "generation-one"
            hard = {"a": 0, "b": 0}
            restores = []
            for role in users:
                (base / ("src-" + role) / "workspace").mkdir(parents=True)
                (base / ("dst-" + role)).mkdir()
            def mount(phase, role):
                return base / (phase + "-" + role)

            def snapshot(source, owner):
                return dict(identity=owner, hard_bytes=2_000_000_000,
                            source_filesystem_uuid=source.name)

            def read(target, owner):
                role = owner["user_id"][-1]
                return dict(filesystem_uuid=target.name, volume_label="m5quota-" + target.name,
                            uid=owner["uid"], accounting=True, enforced=True, used_bytes=0, hard_bytes=hard[role])

            def tree(path, owner):
                return {"tree_sha256": owner["workspace"]}

            def save(source, owner, package, reserve_bytes):
                package.mkdir(mode=0o700)
                archive = package / "workspace.tar"
                archive.write_bytes(owner["workspace"].encode())
                archive.chmod(0o600)
                meta = dict(quota=snapshot(source, owner), inventory=tree(source, owner),
                            archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
                lifecycle.write_record(package / "manifest.json", meta)
                pin = hashlib.sha256((package / "manifest.json").read_bytes()).hexdigest()
                return dict(inventory=meta["inventory"], quota=meta["quota"], manifest_sha256=pin)

            def restore(target, owner, package, pin):
                role = owner["user_id"][-1]
                hard[role] = 2_000_000_000
                restores.append(role)
                if restores == ["a", "b"]:
                    raise OSError(errno.ENOSPC, "B out of space")
                (target / "workspace").mkdir(mode=0o700)
                return dict(inventory=tree(target, owner), quota=read(target, owner))

            with (patch.object(lifecycle, "mount", mount),
                  patch.object(lifecycle.quota, "read", read),
                  patch.object(lifecycle.quota, "snapshot", snapshot),
                  patch.object(lifecycle, "inventory", tree),
                  patch.object(lifecycle, "notebook", side_effect=lambda phase, role, owner: {"uid": owner["uid"]}),
                  patch.object(lifecycle, "backup", side_effect=save) as backup,
                  patch.object(lifecycle, "restore", side_effect=restore)):
                with self.assertRaises(OSError):
                    lifecycle.recover_generation(directory, users, users, 16 * 1024**2)
                inode = (mount("dst", "a") / "workspace").stat().st_ino
                retried = lifecycle.recover_generation(directory, users, ["b"], 16 * 1024**2)
                self.assertFalse(retried["b"]["already_restored"])
                self.assertEqual(restores, ["a", "b", "b"])
                checked = lifecycle.recover_generation(directory, users, users, 16 * 1024**2)
                self.assertTrue(all(row["already_restored"] for row in checked.values()))
                self.assertEqual(backup.call_count, 2)
                self.assertEqual((mount("dst", "a") / "workspace").stat().st_ino, inode)
                (directory / "a/package/workspace.tar").write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "Stored archive differs"):
                    lifecycle.recover_generation(directory, users, ["a"], 16 * 1024**2)

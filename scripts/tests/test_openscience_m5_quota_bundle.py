"""A bad or misplaced M5 quota package must fail before the target quota or workspace changes."""

import errno
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openscience_m5_quota_bundle as bundle
import openscience_m5_archive as codec


def dense_quota_package(directory, owner, content, marked):
    """Create a pinned dense TAR the same way the original exporter did."""
    path = directory / "workspace.tar"
    with tarfile.open(path, "w") as archive:
        for name in sorted(codec.SKELETON, key=lambda value: (len(value), value)):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = 0o700
            member.uid = member.gid = owner["uid"]
            if marked and name == ".":
                member.pax_headers[codec.LAYOUT_VERSION] = "1"
            archive.addfile(member)
        for name, data in [(".m1-owner", owner["workspace"].encode()), ("files/science.bin", content)]:
            member = tarfile.TarInfo(name)
            member.mode = 0o444 if name == ".m1-owner" else 0o600
            member.uid = member.gid = 0 if name == ".m1-owner" else owner["uid"]
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    path.chmod(0o600)
    with path.open("rb") as stream:
        tree = codec.process_archive(stream, owner["uid"], owner["workspace"])
    policy = {"identity": owner, "hard_bytes": 4 * 1024**2}
    raw = json.dumps({
        "format": bundle.FORMAT, "quota": policy,
        "inventory": tree, "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }).encode()
    manifest = directory / "manifest.json"
    manifest.write_bytes(raw)
    manifest.chmod(0o600)
    return hashlib.sha256(raw).hexdigest(), tree


class BundleGuardTests(unittest.TestCase):
    def test_quota_restore_preserves_dense_layout_unless_legacy_needs_space(self):
        # Local ordinary files exercise real tar extraction. Mock only ext4 quota,
        # ownership and observability that require root and mounted quota devices.
        owner = {"environment": "dev-m1", "user_id": "test-user-a", "uid": 230001, "workspace": "a" * 32}
        content = b"x" + bytes(2 * 1024**2 - 1)
        block = 4096
        hard = 4 * 1024**2
        if not Path("/dev/shm").is_dir():
            self.skipTest("Need two distinct temporary filesystems for quota package guard")
        with TemporaryDirectory(dir="/dev/shm") as pkg, TemporaryDirectory() as tmp:
            package, base = Path(pkg), Path(tmp)
            if package.stat().st_dev == base.stat().st_dev:
                self.skipTest("Package and restore must reside on distinct devices")
            for case in ("fits", "uid", "filesystem", "actual_enospc", "new_dense"):
                with self.subTest(case=case):
                    mount = base / case
                    mount.mkdir()
                    pin, tree = dense_quota_package(package, owner, content, marked=case == "new_dense")
                    used = 3 * 1024**2 if case in ("uid", "new_dense") else 0
                    vfs = SimpleNamespace(f_frsize=block, f_bavail=32 if case == "filesystem" else 16 * 1024)
                    attempts = []

                    def extract(stream, uid, workspace, target=None, **kwargs):
                        if target is not None:
                            if attempts:
                                self.assertFalse(attempts[-1][0].parent.exists())
                            attempts.append((target, kwargs["recover_legacy_zeros"]))
                            if case == "actual_enospc" and len(attempts) == 1:
                                (target / "partial").touch()
                                raise OSError(errno.ENOSPC, "staging full")
                        return codec.process_archive(stream, uid, workspace, target, **kwargs)

                    def kernel(*args):
                        restored = attempts[-1][0] / "files/science.bin"
                        return {"hard_bytes": hard, "used_bytes": used + restored.stat().st_blocks * 512}

                    with (
                        patch.object(bundle, "process_archive", side_effect=extract),
                        patch.object(bundle.os, "chown"), patch.object(bundle, "inventory", return_value=tree),
                        patch.object(bundle.os, "statvfs", return_value=vfs),
                        patch.object(bundle.quota, "restore", return_value={"hard_bytes": hard, "used_bytes": used}),
                        patch.object(bundle.quota, "read", side_effect=kernel),
                    ):
                        if case == "new_dense":
                            with self.assertRaises(OSError) as error:
                                bundle.restore(mount, owner, package, pin)
                            self.assertEqual(error.exception.errno, errno.EDQUOT)
                            self.assertEqual([r for _, r in attempts], [False])
                            self.assertEqual(list(mount.iterdir()), [])
                            continue
                        result = bundle.restore(mount, owner, package, pin)
                    file = mount / "workspace/files/science.bin"
                    self.assertEqual(file.read_bytes(), content)
                    rescue = case != "fits"
                    self.assertEqual(result.get("legacy_zero_recovery", False), rescue)
                    self.assertEqual(file.stat().st_blocks * 512 < len(content), rescue)
                    self.assertEqual([r for _, r in attempts], [False, True] if case == "actual_enospc" else [rescue])

    def test_wrong_pin_or_owner_cannot_reach_quota_restore(self):
        owner = {
            "environment": "dev-m1",
            "user_id": "test-user-a",
            "workspace": "a" * 32,
            "uid": 230001,
        }
        manifest = {"format": bundle.FORMAT, "quota": {"identity": owner}}
        with (
            TemporaryDirectory() as base,
            patch.object(bundle.quota, "restore") as restore,
        ):
            root = Path(base)
            package, target = root / "package", root / "target"
            package.mkdir(mode=0o700)
            target.mkdir()
            raw = json.dumps(manifest).encode()
            (package / "manifest.json").write_bytes(raw)
            (package / "manifest.json").chmod(0o600)
            pin = hashlib.sha256(raw).hexdigest()
            for identity, supplied_pin in [
                (owner, "0" * 64),
                ({**owner, "user_id": "test-user-b"}, pin),
            ]:
                with (
                    self.subTest(identity=identity, supplied_pin=supplied_pin),
                    self.assertRaises(ValueError),
                ):
                    bundle.restore(target, identity, package, supplied_pin)
            restore.assert_not_called()
            self.assertEqual(list(target.iterdir()), [])

    def test_source_rejects_nested_bind_mount_on_same_device(self):
        with TemporaryDirectory() as base:
            mount = Path(base)
            source = mount / "workspace"
            source.mkdir()
            inside = str(source / "project space").replace(" ", r"\040")
            records = f"9 1 8:1 /shared {inside} rw - ext4 /dev/vdb rw\n"
            with patch.object(Path, "read_text", return_value=records):
                with self.assertRaisesRegex(ValueError, "nested mount"):
                    bundle.check_source_mount(source, mount)

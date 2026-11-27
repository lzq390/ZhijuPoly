import hashlib
import io
import json
import os
from contextlib import ExitStack
from pathlib import Path
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openscience_m5_archive as codec
import openscience_m5_backup as backup

UID = 221234
WORKSPACE = "6" * 32
DEPLOYMENT = "a" * 32


def make_archive(path, *, additions=(), marker=WORKSPACE, omit=None):
    with tarfile.open(path, mode="w") as tar:
        rows = [
            (name, tarfile.DIRTYPE, b"")
            for name in sorted(codec.SKELETON, key=lambda name: (len(name), name))
        ]
        rows += [
            (".m1-owner", tarfile.REGTYPE, marker.encode()),
            ("files/example.txt", tarfile.REGTYPE, b"fixture"),
            *additions,
        ]
        for name, kind, data in rows:
            if name == omit:
                continue
            member = tarfile.TarInfo(name)
            member.uid = member.gid = 0 if name == ".m1-owner" else UID
            member.mode = 0o444 if name == ".m1-owner" else 0o700 if kind == tarfile.DIRTYPE else 0o600
            member.type = kind
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    path.chmod(0o600)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.package = self.root / "package"
        self.package.mkdir(mode=0o700)
        self.archive = self.package / "workspace.tar"
        make_archive(self.archive)

    def inventory(self):
        with self.archive.open("rb") as stream:
            return codec.process_archive(stream, UID, WORKSPACE)

    def manifest(self, **changes):
        instance = {
            "format": backup.SCOPE,
            "scope": backup.LAYOUT,
            "workspace": WORKSPACE,
            "uid": UID,
            "generation": 9,
            "source_deployment": DEPLOYMENT,
            "source_volume": f"nexpoly-m1-{DEPLOYMENT[:12]}-{WORKSPACE}-data",
            "environment": "dev-test",
            "user_id": "test-a",
            "archive_sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            "registry_settings": {"template_version": backup.m1.VERSION},
            "capacity": dict(backup.m1.DEFAULT_CAPACITY),
            "inventory": self.inventory(),
            **changes,
        }
        blob = self.package / "manifest.json"
        blob.write_text(json.dumps(instance))
        blob.chmod(0o600)
        return hashlib.sha256(blob.read_bytes()).hexdigest()

    def test_manifest_pin_checked_before_registry_or_docker(self):
        self.manifest()
        with (
            patch.object(backup.m1, "docker", side_effect=AssertionError("no docker")),
            self.assertRaisesRegex(backup.m1.Problem, "trusted digest"),
        ):
            backup.restore(self.root / "unused", "dev-test", [("test-a", self.package, "0" * 64)])
        self.assertFalse((self.root / "unused").exists())

    def test_swapped_owner_denied_before_registry_or_docker(self):
        pin = self.manifest()
        with (
            patch.object(backup.m1, "docker", side_effect=AssertionError("no docker")),
            self.assertRaisesRegex(backup.m1.Problem, "requested owner"),
        ):
            backup.restore(self.root / "unused", "dev-test", [("test-b", self.package, pin)])
        self.assertFalse((self.root / "unused").exists())

    def test_corrupted_package_is_rejected(self):
        pin = self.manifest()
        with self.archive.open("ab") as output:
            output.write(b"unexpected")
        with ExitStack() as stack, self.assertRaisesRegex(backup.m1.Problem, "Archive digest"):
            backup.verify_package(stack, "dev-test", "test-a", self.package, pin)

    def test_capacity_is_required_and_may_be_stricter_than_default(self):
        pin = self.manifest(capacity={})
        with ExitStack() as stack, self.assertRaisesRegex(backup.m1.Problem, "capacity"):
            backup.verify_package(stack, "dev-test", "test-a", self.package, pin)
        limits = dict(backup.m1.DEFAULT_CAPACITY, volume_free_floor_mib=65536)
        pin = self.manifest(capacity=limits)
        with ExitStack() as stack:
            data, archive = backup.verify_package(stack, "dev-test", "test-a", self.package, pin)
            self.assertEqual(data["capacity"], limits)
            space = backup.restore_space_required([(data, archive)])
            self.assertGreater(space, self.archive.stat().st_size)
            self.assertEqual(backup.restore_space_required([(data, archive)] * 2), 2 * space)

    def test_dense_version_one_package_still_matches_its_original_tree_digest(self):
        self.assertEqual(
            hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            "c48237ec1e110bf14d6a2bdd986b201484b23989ccc2ac315903aac68fa69d7e",
        )
        legacy_tree = {
            "tree_sha256": "76ddb223bcf24fefb9c546683f135f8e5e7513364e3794666e1952cd359c8a87",
            "files": 2,
            "directories": 9,
            "file_bytes": 39,
        }
        pin = self.manifest(inventory=legacy_tree)
        restored = self.root / "restore"
        restored.mkdir()
        with ExitStack() as stack:
            _, stream = backup.verify_package(stack, "dev-test", "test-a", self.package, pin)
            with patch.object(codec.os, "chown"):
                self.assertEqual(codec.process_archive(stream, UID, WORKSPACE, restored), legacy_tree)
        self.assertEqual((restored / "files/example.txt").read_bytes(), b"fixture")

    def test_restore_space_counts_partial_sparse_blocks_and_rewinds_input(self):
        block = 4096
        # Two bytes share a block. The other bytes are in independent blocks
        # separated by large holes. The logical file length should not dominate
        # admission, and compact payload size alone would underestimate it.
        offsets = [0, 3, *(i * 1024**2 + 1 for i in range(1, 17))]
        with tarfile.open(self.archive, mode="w") as tar:
            member = tarfile.TarInfo("files/regions")
            member.size = len(offsets)
            member.pax_headers = {
                "GNU.sparse.size": str(offsets[-1] + 1),
                "GNU.sparse.name": member.name,
                "GNU.sparse.numblocks": str(len(offsets)),
                "GNU.sparse.map": ",".join(f"{offset},1" for offset in offsets),
            }
            tar.addfile(member, io.BytesIO(b"x" * len(offsets)))
        with self.archive.open("rb") as stream:
            verified = [({"inventory": {"files": 1, "directories": 0}}, stream)]
            reserved = backup.restore_space_required(verified)
            self.assertEqual(stream.tell(), 0)
        self.assertEqual(reserved, self.archive.stat().st_size + block + 17 * block - len(offsets))
        self.assertLess(reserved, offsets[-1] + 1)

    def test_restore_space_counts_unwritten_reservations_past_eof_and_shared_blocks(self):
        block = 4096
        with tarfile.open(self.archive, mode="w") as tar:
            dense = tarfile.TarInfo("files/dense")
            dense.size = 3
            dense.pax_headers[codec.RESERVATION_MAP] = "4096,8192"
            tar.addfile(dense, io.BytesIO(b"abc"))
            empty = tarfile.TarInfo("files/empty")
            empty.pax_headers[codec.RESERVATION_MAP] = "16777216,4096"
            tar.addfile(empty)
            sparse = tarfile.TarInfo("files/sparse")
            sparse.size = 1
            sparse.pax_headers = {
                "GNU.sparse.size": "1048576", "GNU.sparse.name": sparse.name,
                "GNU.sparse.numblocks": "1", "GNU.sparse.map": "0,1",
                codec.RESERVATION_MAP: "1,128,16777216,8192",
            }
            tar.addfile(sparse, io.BytesIO(b"x"))
        with self.archive.open("rb") as stream:
            required = backup.restore_space_required([({"inventory": {"files": 3, "directories": 0}}, stream)])
            self.assertEqual(stream.tell(), 0)
        # Dense: three occupied blocks. Empty: one. Sparse: one shared by
        # data and a reservation, plus two beyond EOF. Four bytes were packed.
        self.assertEqual(required, self.archive.stat().st_size + 3 * block + 7 * block - 4)

    def test_scope_and_complete_private_inventory(self):
        results = self.inventory()
        self.assertEqual(results["directories"], len(codec.SKELETON))
        self.assertEqual(results["files"], 2)
        self.assertEqual(results["file_bytes"], len(WORKSPACE) + 7)
        for name in ("files/new-owner-content", "records/work/project.json", "files/.other"):
            self.assertTrue(codec.permitted(name))
        for name in ("files/.runtime", "files/.runtime/data/log", "config/service-auth", "home/private"):
            self.assertFalse(codec.permitted(name))

    def test_unknown_root_tree_is_not_omitted(self):
        data = self.root / "data"
        data.mkdir()
        (data / "future").mkdir()
        with self.assertRaisesRegex(ValueError, "Unrecognized"):
            list(codec.entries(data))

    def test_required_root_or_marker_wrong(self):
        make_archive(self.archive, omit="files")
        with self.assertRaises(ValueError):
            self.inventory()
        make_archive(self.archive, marker="4" * 32)
        with self.assertRaisesRegex(ValueError, "identity marker"):
            self.inventory()

    def test_extraction_accepts_only_scoped_unique_regular_entries(self):
        for name, kind in (
            ("files/example.txt", tarfile.REGTYPE),
            ("files/link", tarfile.SYMTYPE),
            ("files/.runtime/secret", tarfile.REGTYPE),
            ("files/../state", tarfile.DIRTYPE),
        ):
            with self.subTest(name=name):
                make_archive(self.archive, additions=[(name, kind, b"")])
                with self.assertRaises(ValueError):
                    self.inventory()

    def test_no_extraction_into_populated_volume(self):
        keep = self.root / "keep"
        keep.write_bytes(b"existing")
        with self.archive.open("rb") as stream, self.assertRaisesRegex(ValueError, "empty volume"):
            codec.process_archive(stream, UID, WORKSPACE, self.root)
        self.assertEqual(keep.read_bytes(), b"existing")

    def test_backup_requires_distinct_direct_physical_disks(self):
        sys_block = self.root / "sys-block"
        sys_block.mkdir()

        def disk(name, base, controller):
            node = self.root / name
            node.mkdir()
            (node / "slaves").mkdir()
            (node / "dev").write_text(base)
            hw = self.root / controller
            hw.mkdir(exist_ok=True)
            (node / "device").symlink_to(hw)
            (sys_block / base).symlink_to(node)
            return node

        first = disk("disk1", "1:0", "adapter1")
        second = disk("disk2", "2:0", "adapter2")
        first_part = first / "part2"
        first_part.mkdir()
        (first_part / "partition").touch()
        (sys_block / "1:2").symlink_to(first_part)
        a, b = backup.separate_disks("1:2", "2:0", sys_block)
        self.assertEqual((a["device"], b["device"]), ("1:0", "2:0"))
        with self.assertRaisesRegex(backup.m1.Problem, "separate physical disk"):
            backup.separate_disks("1:2", "1:0", sys_block)
        with self.assertRaises(backup.m1.Problem):
            backup.separate_disks("1:2", "0:43", sys_block)
        (second / "slaves" / "another-layer").touch()
        with self.assertRaisesRegex(backup.m1.Problem, "direct persistent"):
            backup.separate_disks("1:2", "2:0", sys_block)

    def test_restore_fsyncs_every_object_and_propagates_failure(self):
        for fail in (False, True):
            dest = self.root / ("failed" if fail else "complete")
            dest.mkdir()
            flushed = []

            def sync(fd):
                name = os.readlink("/proc/self/fd/" + str(fd))
                flushed.append(Path(name).relative_to(dest))
                if fail:
                    raise OSError("volume sync failed")

            with (
                self.archive.open("rb") as stream,
                patch.object(codec.os, "chown"),
                patch.object(codec.os, "fsync", side_effect=sync),
            ):
                if fail:
                    with self.assertRaisesRegex(OSError, "volume sync failed"):
                        codec.process_archive(stream, UID, WORKSPACE, dest)
                else:
                    result = codec.process_archive(stream, UID, WORKSPACE, dest)
                    self.assertEqual(len(flushed), result["files"] + result["directories"])
                    for index, path in enumerate(flushed[:-1]):
                        self.assertGreater(flushed.index(path.parent), index)
                    self.assertEqual(flushed[-1], Path("."))

    def test_existing_empty_publication_target_is_preserved(self):
        src = self.root / "stage"
        src.mkdir()
        existing = self.root / "final"
        existing.mkdir()
        ino = existing.stat().st_ino
        with self.assertRaises(FileExistsError):
            backup.publish(src, existing)
        self.assertTrue(src.is_dir())
        self.assertEqual(existing.stat().st_ino, ino)

    def test_low_disk_abort_before_archive_write(self):
        output = io.BytesIO()
        with (
            patch.object(
                backup,
                "available_bytes",
                side_effect=[backup.BACKUP_FREE_FLOOR + backup.CHUNK, backup.BACKUP_FREE_FLOOR],
            ),
            self.assertRaisesRegex(backup.m1.Problem, "Insufficient"),
        ):
            backup.stream_backup([sys.executable, "-c", "print('test')"], output, self.root)
        self.assertEqual(output.getvalue(), b"")
        backup.require_space(115, 15, 100)
        with self.assertRaises(backup.m1.Problem):
            backup.require_space(114, 15, 100)

    def test_incomplete_cleanup_refuses_unrelated_volume(self):
        from unittest.mock import Mock

        workspace = Mock(volume="test-volume")
        workspace.resource.return_value = "test-volume"
        with (
            patch.object(backup.m1, "docker", side_effect=["", "another-run"]) as docker,
            self.assertRaisesRegex(backup.m1.Problem, "unrelated"),
        ):
            backup.delete_owned_empty_instance_volume(workspace, "this-run")
        self.assertFalse(any(c.args[:2] == ("volume", "rm") for c in docker.call_args_list))


if __name__ == "__main__":
    unittest.main()

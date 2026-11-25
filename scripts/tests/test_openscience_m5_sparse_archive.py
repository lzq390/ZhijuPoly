"""Quota-compatible streaming archives for sparse workspace files."""

import errno
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openscience_m5_archive as codec
from openscience_m5_quota_bundle import archived_reservations


UID = os.getuid()
WORKSPACE = "9" * 32
MIB = 1024**2


def add_skeleton(tar, current_root=None):
    # Build a real root header through the writer when testing the current format.
    if current_root is not None:
        codec.add_tree(current_root, tar, UID, WORKSPACE)
    for name in sorted(codec.SKELETON, key=lambda n: (len(n), n)):
        if name == "." and current_root is not None:
            continue
        entry = tarfile.TarInfo(name)
        entry.type, entry.mode, entry.uid, entry.gid = tarfile.DIRTYPE, 0o700, UID, UID
        tar.addfile(entry)
    entry = tarfile.TarInfo(".m1-owner")
    entry.mode, entry.size = 0o444, len(WORKSPACE)
    tar.addfile(entry, io.BytesIO(WORKSPACE.encode()))


def user_file(name, size):
    entry = tarfile.TarInfo("files/" + name)
    entry.mode, entry.size, entry.uid, entry.gid = 0o600, size, UID, UID
    return entry


class SparseArchiveTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def package(self, name, fill, *, current=False):
        path = self.root / name
        source_root = self.root / "empty-source"
        source_root.mkdir(mode=0o700, exist_ok=True)
        with tarfile.open(path, "w|", format=tarfile.PAX_FORMAT) as tar:
            add_skeleton(tar, source_root if current else None)
            fill(tar)
        return path

    def restore(self, package, dest, **options):
        dest.mkdir()
        # The operator runs this on a guest as root; this test only needs to
        # intercept assignment of the root-owned marker on unprivileged hosts.
        with package.open("rb") as src, patch.object(os, "chown"):
            return codec.process_archive(src, UID, WORKSPACE, dest, **options)

    def test_compact_tar_restores_data_holes_and_legacy_logical_digests(self):
        source = self.root / "input"
        source.mkdir()
        cases = {
            "scattered": (8 * MIB, [(MIB + 37, b"begin"), (3 * MIB, bytes(4096)), (6 * MIB, b"end")]),
            "all-hole": (2 * MIB, []),
            "hole-after-data": (MIB + 1, [(0, b"x")]),
            "hole-before-data": (MIB + 1, [(MIB, b"y")]),
            "empty": (0, []),
            "dense": (4096, [(0, bytes(4096))]),
        }
        for name, (logical, contents) in cases.items():
            with (source / name).open("wb") as file:
                for offset, data in contents:
                    file.seek(offset)
                    file.write(data)
                file.truncate(logical)
                file.flush()
                os.fsync(file.fileno())

        def pack(tar, detect):
            for name in sorted(cases):
                path = source / name
                entry = user_file(name, path.stat().st_size)
                with path.open("rb", buffering=0) as reader:
                    if detect:
                        codec.add_regular(tar, entry, reader)
                    else:
                        tar.addfile(entry, reader)

        compact = self.package("sparse.tar", lambda tar: pack(tar, True), current=True)
        legacy = self.package("legacy.tar", lambda tar: pack(tar, False))
        with legacy.open("rb") as stream:
            self.assertEqual(archived_reservations(stream), {})
        with compact.open("rb") as stream:
            maps = archived_reservations(stream)
            expected = {f"files/{name}": [] for name in cases}
            expected[".m1-owner"] = []
            self.assertEqual(maps, expected)
        self.assertLess(compact.stat().st_size, MIB)
        self.assertGreater(legacy.stat().st_size, 12 * MIB)
        restored = self.root / "restored"
        report = self.restore(compact, restored)
        with legacy.open("rb") as archive:
            previous = codec.process_archive(archive, UID, WORKSPACE)
        self.assertEqual(report, previous)
        self.assertEqual(report["file_bytes"], sum(c[0] for c in cases.values()) + len(WORKSPACE))

        for name in cases:
            before, after = source / name, restored / "files" / name
            with self.subTest(name=name), before.open("rb") as left, after.open("rb") as right:
                self.assertEqual(hashlib.file_digest(left, "sha256").digest(), hashlib.file_digest(right, "sha256").digest())
                self.assertEqual(after.stat().st_blocks, before.stat().st_blocks)
                self.assertEqual(codec.data_extents(left, before.stat().st_size),
                                 codec.data_extents(right, after.stat().st_size))
        # Check with a reader independent of our parser.
        with tarfile.open(compact) as archive:
            for name in cases:
                self.assertEqual(archive.extractfile("files/" + name).read(), (source / name).read_bytes())
        if shutil.which("tar"):
            reference = self.root / "reference"
            reference.mkdir()
            subprocess.run(["tar", "-xf", str(compact), "--no-same-owner", "files/scattered"],
                           cwd=reference, capture_output=True, check=True)
            actual = reference / "files/scattered"
            self.assertEqual(actual.read_bytes(), (source / "scattered").read_bytes())
            self.assertEqual(actual.stat().st_blocks, (source / "scattered").stat().st_blocks)

    def test_reconstruct_legacy_zero_blocks_only_on_opt_in_and_never_in_modern_dense_members(self):
        block = codec.LEGACY_BLOCK
        contents = bytearray(2 * MIB + 11)
        # Adjacent data in the last block of one read chunk and first block of
        # the next must survive as one run; leading and trailing bytes are zero.
        contents[MIB - 2:MIB + 2] = b"edge"

        def fill(tar):
            tar.addfile(user_file("expanded", len(contents)), io.BytesIO(contents))
            tar.addfile(user_file("zeros", 3 * block), io.BytesIO(bytes(3 * block)))

        previous = self.package("old.tar", fill)
        current = self.package("current.tar", fill, current=True)
        expected = None
        for name, package, rescue, sparse in (
            ("old-normal", previous, False, False), ("old-rescue", previous, True, True),
            ("current-rescue", current, True, False),
        ):
            with self.subTest(name=name):
                layout = {}
                def record(member, ranges, legacy):
                    layout[member.name] = (ranges, legacy)
                options = dict(allocation=record, recover_legacy_zeros=rescue)
                with package.open("rb") as stream:
                    verified = codec.process_archive(stream, UID, WORKSPACE, **options)
                verified_layout = dict(layout)
                restored = self.root / name
                report = self.restore(package, restored, **options)
                self.assertEqual(layout, verified_layout)
                self.assertEqual(report, verified)
                if expected is None:
                    expected = report
                self.assertEqual(report, expected)
                data, zeros = restored / "files/expanded", restored / "files/zeros"
                self.assertEqual(data.read_bytes(), contents)
                self.assertEqual(zeros.read_bytes(), bytes(3 * block))
                self.assertEqual(layout["files/expanded"][1], package == previous)
                if sparse:
                    self.assertEqual(data.stat().st_blocks * 512, 2 * block)
                    self.assertEqual(zeros.stat().st_blocks, 0)
                    self.assertEqual(layout["files/expanded"][0], [(MIB - block, 2 * block)])
                else:
                    self.assertGreaterEqual(data.stat().st_blocks * 512, len(contents))
                    self.assertEqual(zeros.stat().st_blocks * 512, 3 * block)
                    self.assertEqual(layout["files/expanded"][0], [(0, len(contents))])

    def test_ext4_unwritten_and_keep_size_reservations_survive_beside_data_and_holes(self):
        unit = 4096
        mixed, empty = self.root / "mixed", self.root / "empty"
        with mixed.open("wb", buffering=0) as handle:
            os.posix_fallocate(handle.fileno(), 0, 2 * unit)
            handle.write(b"data")
            os.posix_fallocate(handle.fileno(), 4 * unit, 2 * unit)
            handle.truncate(9 * unit + 7)
        empty.touch()
        for path in (mixed, empty):
            subprocess.run(["fallocate", "--keep-size", "--offset", str(16 * unit),
                            "--length", str(3 * unit), str(path)], check=True)

        def fill(tar):
            for path in (mixed, empty):
                with path.open("rb", buffering=0) as source:
                    codec.add_regular(tar, user_file(path.name, path.stat().st_size), source)

        expected = {
            "mixed": ([(0, unit)], [(unit, unit), (4 * unit, 2 * unit), (16 * unit, 3 * unit)]),
            "empty": ([], [(16 * unit, 3 * unit)]),
        }
        package = self.package("reservations.tar", fill, current=True)
        with package.open("rb") as stream:
            maps = archived_reservations(stream)
            for name, (_, ranges) in expected.items():
                self.assertEqual(maps["files/" + name], ranges)
        measured = {}
        destination = self.root / "reservations"
        self.restore(package, destination, allocation=lambda m, r, old: measured.update({m.name: r}))
        with tarfile.open(package) as reader:
            for path in (mixed, empty):
                restored = destination / "files" / path.name
                data, reservations = expected[path.name]
                with self.subTest(name=path.name), path.open("rb") as left, restored.open("rb") as right:
                    self.assertEqual(codec.fiemap_layout(left.fileno(), path.stat().st_size), (data, reservations))
                    self.assertEqual(codec.fiemap_layout(right.fileno(), restored.stat().st_size), (data, reservations))
                    self.assertEqual(restored.stat().st_size, path.stat().st_size)
                    self.assertEqual(restored.stat().st_blocks, path.stat().st_blocks)
                    self.assertEqual(restored.read_bytes(), path.read_bytes())
                    self.assertEqual(codec.reservation_extents(reader.getmember("files/" + path.name)), reservations)
                    self.assertEqual(measured["files/" + path.name], codec.allocated_ranges(data, reservations))
        self.assertLess(package.stat().st_size, 8 * unit)

    def test_only_unwritten_flag_denotes_reserved_storage(self):
        def merged_mapping(_fd, _op, buffer, _mutate):
            size = 4096
            codec.FIEMAP_HEADER.pack_into(buffer, 0, 0, 2**64-1, codec.FIEMAP_SYNC, 1, codec.FIEMAP_BATCH, 0)
            codec.FIEMAP_EXTENT.pack_into(buffer, codec.FIEMAP_HEADER.size, 0, size, size, 0, 0,
                                          0x1000 | codec.FIEMAP_LAST, 0, 0, 0)
        with patch.object(codec.fcntl, "ioctl", side_effect=merged_mapping):
            self.assertEqual(codec.fiemap_layout(1, 4096), ([(0, 4096)], []))

    def test_invalid_reservations_and_inconsistent_readback_are_refused(self):
        # The one-byte payload belongs to a normal, dense file.
        for label, reservation in (("overlap", "0,4096"), ("negative", "-1,32"),
                                   ("zero", "6,0"), ("missing", "5"), ("order", "50,10,40,5"),
                                   ("overflow", f"{2**63 - 1},4096")):
            def fill(tar):
                member = user_file("kept", 1)
                member.pax_headers[codec.RESERVATION_MAP] = reservation
                tar.addfile(member, io.BytesIO(b"x"))
            package = self.package(label + ".tar", fill, current=True)
            dest = self.root / label
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.restore(package, dest)
            self.assertFalse((dest / "files/kept").exists())
        with (self.root / "verification-failure").open("w+b") as sink:
            with patch.object(codec, "fiemap_layout", return_value=([], [])):
                with self.assertRaisesRegex(ValueError, "Restored unwritten extents"):
                    codec.restore_reservations(sink, 0, [(4096, 4096)])

    def test_invalid_maps_rejected_before_target_file_creation(self):
        # Physical payload is four bytes; include same-512-byte-block mismatches
        # to ensure validation checks the exact header size rather than padding.
        for label, offsets, count in (
            ("out-of-order", "20,2,10,2", "2"),
            ("overlap", "10,3,12,1", "2"),
            ("outside", "62,4", "1"),
            ("negative", "2,-1", "1"),
            ("incomplete-offset", "10,4,5", "1"),
            ("wrong-count", "10,4", "2"),
            ("short-payload", "10,5", "1"),
            ("long-payload", "10,3", "1"),
            ("interior-empty", "10,0,12,4", "2"),
        ):
            with self.subTest(label=label):
                def fill(tar):
                    member = user_file("data", 4)
                    member.pax_headers = {
                        "GNU.sparse.size": "64", "GNU.sparse.numblocks": count,
                        "GNU.sparse.map": offsets, "GNU.sparse.name": member.name,
                    }
                    tar.addfile(member, io.BytesIO(b"data"))
                package = self.package(label + ".tar", fill)
                dest = self.root / label
                with self.assertRaisesRegex(ValueError, "sparse|Sparse"):
                    self.restore(package, dest)
                self.assertFalse((dest / "files/data").exists())

    def test_unavailable_extent_queries_do_not_flatten_sparse_input(self):
        path = self.root / "test.dat"
        path.write_bytes(b"dense")
        with path.open("r+b", buffering=0) as file:
            with patch.object(os, "lseek", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")):
                self.assertEqual(codec.data_extents(file, 5), [(0, 5)])
                file.truncate(8 * MIB)
                with self.assertRaisesRegex(ValueError, "sparse data extents"):
                    codec.data_extents(file, 8 * MIB)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MEDIA = load_module(
    "postgres_media_evidence_test",
    ROOT / "scripts/postgres_media_evidence.py",
)
CONTRACTS = load_module(
    "postgres_media_site_contract_test",
    ROOT / "scripts/site_helper_contracts.py",
)

IMAGE = "docker.io/library/postgres@sha256:" + "a" * 64
CONTAINER_A = "1" * 64
CONTAINER_B = "2" * 64
CONTAINER_C = "3" * 64


def attachment_record(
    container_id: str,
    *,
    state: str = "running",
    destination: str = "/var/lib/postgresql/data",
) -> dict[str, object]:
    return {
        "container_id": container_id,
        "container_name": f"/fixture-{container_id[:12]}",
        "container_image_id": "sha256:" + "4" * 64,
        "container_config_sha256": "sha256:" + "5" * 64,
        "container_created_at": "2026-07-17T10:00:00.000000000Z",
        "container_started_at": "2026-07-17T10:01:00.000000000Z",
        "container_finished_at": "0001-01-01T00:00:00Z",
        "container_restart_count": 0,
        "state": state,
        "destination": destination,
        "read_only": False,
    }


def descriptor(
    media_id: str,
    database: str,
    *,
    disposition: str = "read-only-online",
    method: str = "live-read-only",
    user: str = "auditor",
    service: str | None = "audit",
    classification: str = "nexpoly-db",
    source_postgres_major: int | None = 16,
) -> dict[str, object]:
    if media_id.startswith("docker-volume:"):
        kind = "docker_volume"
    elif media_id.startswith("container-bind:"):
        kind = "container_bind"
    else:
        kind = "postgres_backup"
        source_postgres_major = None
    return {
        "media_id": media_id,
        "kind": kind,
        "database": database,
        "database_user": user,
        "disposition": disposition,
        "audit_method": method,
        "pg_service": service,
        "classification": classification,
        "source_postgres_major": source_postgres_major,
        "databases": [
            {
                "name": database,
                "oid": str(
                    16000
                    + int(
                        hashlib.sha256(database.encode()).hexdigest()[:4],
                        16,
                    )
                ),
                "owner": user,
                "allow_connections": True,
                "template": False,
                "audit_role": user,
                "migration_scope": "nexpoly-ledger",
            }
        ],
    }


def registry_document(
    policy,
    descriptors: list[dict[str, object]],
    *,
    dev_media: str,
    health_media: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 3,
        "discovery_boundary": MEDIA.seal_discovery_boundary(policy),
        "audit_runtime": {
            "auditor_sha256": MEDIA._auditor_digest(),
            "pg_service_file_sha256": "sha256:" + "7" * 64,
            "postgres_image": IMAGE,
            "postgres_major": 16,
            "postgres_uid": 70,
            "postgres_gid": 70,
        },
        "expected_media": descriptors,
        "required_online_databases": [
            {"stack": "nexpoly_dev", "media_id": dev_media},
            *(
                [
                    {
                        "stack": "nexpoly_md_health_opt",
                        "media_id": health_media,
                    }
                ]
                if health_media is not None
                else []
            ),
        ],
    }


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


def private_file(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    os.chmod(path, 0o600)


def postgres_signature(root: str, major: int = 16) -> str:
    return (
        f"B\t{root}/base\n"
        f"C\t{root}/global/pg_control\n"
        f"V\t{root}/PG_VERSION\t{major}\n"
    )


class RegistryAndPrivatePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="postgres-media-registry-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.backup_a = self.root / "backups-a"
        self.backup_b = self.root / "backups-b"
        self.config = self.root / "config"
        for path in (self.backup_a, self.backup_b, self.config):
            private_directory(path)
        self.backup_a_identity = MEDIA.capture_backup_root_identity(
            self.backup_a
        )
        self.backup_b_identity = MEDIA.capture_backup_root_identity(
            self.backup_b
        )
        self.policy = MEDIA.DiscoveryPolicy(
            backup_roots=(self.backup_a, self.backup_b)
        )
        self.registry = self.config / "registry.json"

    def write_registry(self, value: dict[str, object]) -> None:
        private_file(
            self.registry,
            json.dumps(value, sort_keys=True).encode("utf-8"),
        )

    def valid_document(self) -> dict[str, object]:
        identifiers = [
            "docker-volume:a-production",
            "docker-volume:b-dev",
            "docker-volume:c-health",
        ]
        return registry_document(
            self.policy,
            [
                descriptor(
                    identifiers[0],
                    "nexpoly",
                    disposition="writable-target",
                    user="production_auditor",
                    service="production_audit",
                ),
                descriptor(
                    identifiers[1],
                    "nexpoly_dev",
                    user="dev_auditor",
                    service="dev_audit",
                ),
                descriptor(
                    identifiers[2],
                    "nexpoly_md_health_opt",
                    disposition="retained-private-isolated",
                    method="isolated-volume-copy-read-only",
                    user="postgres",
                    service=None,
                ),
            ],
            dev_media=identifiers[1],
        )

    def test_default_boundary_covers_every_known_private_backup_root(self) -> None:
        self.assertEqual(
            MEDIA.DiscoveryPolicy().document()["backup_roots"],
            [
                "/data/lzq/gith/nexpoly/backups",
                (
                    "/data/lzq/gith/nexpoly-runtime/legacy-takeover/"
                    "preserved-postgres-backups"
                ),
                "/data/lzq/recovery/nexpoly-postgres-media",
                (
                    "/data/lzq/recovery/"
                    "nexpoly-pre-merge-20260717T090623Z/"
                    "dev-0009-quarantine"
                ),
            ],
        )

    def test_registry_v3_accepts_only_the_complete_compiled_boundary(self) -> None:
        value = self.valid_document()
        self.write_registry(value)
        loaded = MEDIA.load_registry(
            self.registry,
            policy=self.policy,
            private_root=self.config,
        )
        self.assertEqual(
            loaded.boundary,
            value["discovery_boundary"],
        )
        self.assertEqual(
            [record.media_id for record in loaded.descriptors],
            sorted(record["media_id"] for record in value["expected_media"]),
        )

        narrowed = copy.deepcopy(value)
        narrowed["discovery_boundary"]["backup_roots"] = [str(self.backup_a)]
        self.write_registry(narrowed)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "narrowed or changed",
        ):
            MEDIA.load_registry(
                self.registry,
                policy=self.policy,
                private_root=self.config,
            )

        missing_identity = copy.deepcopy(value)
        missing_identity["discovery_boundary"].pop(
            "backup_root_identities"
        )
        self.write_registry(missing_identity)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "narrowed or changed",
        ):
            MEDIA.load_registry(
                self.registry,
                policy=self.policy,
                private_root=self.config,
            )

        replaced_identity = copy.deepcopy(value)
        replaced_identity["discovery_boundary"][
            "backup_root_identities"
        ][0]["inode"] += 1
        self.write_registry(replaced_identity)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "identity differs",
        ):
            MEDIA.load_registry(
                self.registry,
                policy=self.policy,
                private_root=self.config,
            )

    def test_registry_rejects_postgres_runtime_uid_gid_drift(self) -> None:
        for field in ("postgres_uid", "postgres_gid"):
            for drift in (999, 70.0, True):
                value = self.valid_document()
                value["audit_runtime"][field] = drift
                self.write_registry(value)
                with self.assertRaisesRegex(
                    MEDIA.MediaEvidenceError,
                    "audit runtime",
                ):
                    MEDIA.load_registry(
                        self.registry,
                        policy=self.policy,
                        private_root=self.config,
                    )

    def test_dev_and_health_media_can_both_remain_retained_offline(self) -> None:
        value = self.valid_document()
        development = value["expected_media"][1]
        development.update(
            {
                "database_user": "nexpoly_dev",
                "disposition": "retained-private-isolated",
                "audit_method": "isolated-volume-copy-read-only",
                "pg_service": None,
            }
        )
        development["databases"][0].update(
            {"owner": "nexpoly_dev", "audit_role": "nexpoly_dev"}
        )
        health = value["expected_media"][2]
        health.update(
            {
                "database_user": "postgres",
                "disposition": "retained-private-isolated",
                "audit_method": "isolated-volume-copy-read-only",
                "pg_service": None,
            }
        )
        health["databases"][0].update(
            {"owner": "postgres", "audit_role": "postgres"}
        )
        value["required_online_databases"] = []
        self.write_registry(value)
        loaded = MEDIA.load_registry(
            self.registry,
            policy=self.policy,
            private_root=self.config,
        )
        self.assertEqual(
            loaded.required_online_databases,
            (),
        )

        development.update(
            {
                "database_user": "dev_auditor",
                "disposition": "read-only-online",
                "audit_method": "live-read-only",
                "pg_service": "dev_audit",
            }
        )
        development["databases"][0].update(
            {"owner": "dev_auditor", "audit_role": "dev_auditor"}
        )
        health.update(
            {
                "database_user": "health_auditor",
                "disposition": "read-only-online",
                "audit_method": "live-read-only",
                "pg_service": "health_audit",
            }
        )
        health["databases"][0].update(
            {"owner": "health_auditor", "audit_role": "health_auditor"}
        )
        value["required_online_databases"] = [
            {
                "stack": "nexpoly_md_health_opt",
                "media_id": "docker-volume:c-health",
            },
            {
                "stack": "nexpoly_dev",
                "media_id": "docker-volume:b-dev",
            },
        ]
        self.write_registry(value)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "canonical subset",
        ):
            MEDIA.load_registry(
                self.registry,
                policy=self.policy,
                private_root=self.config,
            )

    def test_adjacent_pg18_is_record_only_but_managed_pg18_is_rejected(
        self,
    ) -> None:
        value = self.valid_document()
        adjacent = {
            **descriptor(
                "docker-volume:d-adjacent-pg18",
                "none",
                disposition="excluded-from-nexpoly-migration",
                method="adjacent-record-only",
                user="none",
                service=None,
                classification="adjacent-record-only",
                source_postgres_major=18,
            ),
            "databases": [],
        }
        value["expected_media"].append(adjacent)
        self.write_registry(value)
        loaded = MEDIA.load_registry(
            self.registry,
            policy=self.policy,
            private_root=self.config,
        )
        self.assertEqual(
            loaded.descriptors[-1].source_postgres_major,
            18,
        )

        value["expected_media"][-1]["source_postgres_major"] = 19
        self.write_registry(value)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "descriptor identity",
        ):
            MEDIA.load_registry(
                self.registry,
                policy=self.policy,
                private_root=self.config,
            )

        value = self.valid_document()
        value["expected_media"][1]["source_postgres_major"] = 18
        self.write_registry(value)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "audit method conflicts",
        ):
            MEDIA.load_registry(
                self.registry,
                policy=self.policy,
                private_root=self.config,
            )

    def test_adjacent_system_backup_has_no_fabricated_database_inventory(
        self,
    ) -> None:
        value = self.valid_document()
        backup = self.backup_a / "postgres.dump"
        adjacent = {
            **descriptor(
                f"postgres-backup:{backup}",
                "none",
                disposition="excluded-from-nexpoly-migration",
                method="adjacent-record-only",
                user="none",
                service=None,
                classification="adjacent-record-only",
            ),
            "databases": [],
        }
        value["expected_media"].append(adjacent)
        value["expected_media"].sort(key=lambda record: record["media_id"])
        self.write_registry(value)
        loaded = MEDIA.load_registry(
            self.registry,
            policy=self.policy,
            private_root=self.config,
        )
        selected = next(
            item
            for item in loaded.descriptors
            if item.media_id == adjacent["media_id"]
        )
        self.assertEqual(selected.kind, "postgres_backup")
        self.assertIsNone(selected.source_postgres_major)
        self.assertEqual(selected.databases, ())

        value["expected_media"][-1]["source_postgres_major"] = 16
        self.write_registry(value)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "audit method conflicts",
        ):
            MEDIA.load_registry(
                self.registry,
                policy=self.policy,
                private_root=self.config,
            )

    def test_private_open_rejects_group_access_hardlinks_and_symlink_parent(self) -> None:
        value = self.valid_document()
        self.write_registry(value)
        os.chmod(self.registry, 0o640)
        with self.assertRaisesRegex(MEDIA.MediaEvidenceError, "unsafe"):
            MEDIA.load_registry(
                self.registry,
                policy=self.policy,
                private_root=self.config,
            )
        os.chmod(self.registry, 0o600)
        hardlink = self.config / "registry-hardlink.json"
        os.link(self.registry, hardlink)
        with self.assertRaisesRegex(MEDIA.MediaEvidenceError, "unsafe"):
            MEDIA.load_registry(
                self.registry,
                policy=self.policy,
                private_root=self.config,
            )
        hardlink.unlink()
        outside = self.root / "outside"
        private_directory(outside)
        link = self.root / "linked-config"
        link.symlink_to(self.config, target_is_directory=True)
        with self.assertRaises(OSError):
            MEDIA.open_private_regular(
                link / "registry.json",
                root=link,
            )

    def test_sealed_backup_root_tolerates_shared_ancestors_only_by_inode(
        self,
    ) -> None:
        shared = self.root / "shared"
        private_directory(shared)
        os.chmod(shared, 0o770)
        sealed = shared / "sealed"
        private_directory(sealed)
        backup = sealed / "postgres.dump"
        private_file(backup, b"PGDMP fixture")
        authority = MEDIA.capture_backup_root_identity(sealed)

        descriptor_fd = MEDIA.open_sealed_backup_regular(
            backup,
            root=sealed,
            root_authority=authority,
        )
        try:
            self.assertEqual(os.read(descriptor_fd, 64), b"PGDMP fixture")
        finally:
            os.close(descriptor_fd)

        old_shared = self.root / "shared-old"
        shared.rename(old_shared)
        private_directory(shared)
        os.chmod(shared, 0o770)
        replacement = shared / "sealed"
        private_directory(replacement)
        private_file(replacement / "postgres.dump", b"replacement")
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "identity differs",
        ):
            MEDIA.open_sealed_backup_regular(
                replacement / "postgres.dump",
                root=replacement,
                root_authority=authority,
            )

    def test_sealed_backup_root_rejects_symlink_and_survives_inflight_rename(
        self,
    ) -> None:
        shared = self.root / "rename-shared"
        private_directory(shared)
        os.chmod(shared, 0o770)
        sealed = shared / "sealed"
        private_directory(sealed)
        backup = sealed / "postgres.dump"
        private_file(backup, b"original")
        authority = MEDIA.capture_backup_root_identity(sealed)
        moved = shared / "sealed-moved"
        original_open = MEDIA._open_directory_without_symlinks
        renamed = False

        def open_then_rename(path: Path) -> int:
            nonlocal renamed
            descriptor_fd = original_open(path)
            if path == sealed and not renamed:
                sealed.rename(moved)
                renamed = True
            return descriptor_fd

        with mock.patch.object(
            MEDIA,
            "_open_directory_without_symlinks",
            side_effect=open_then_rename,
        ):
            descriptor_fd = MEDIA.open_sealed_backup_regular(
                backup,
                root=sealed,
                root_authority=authority,
            )
        try:
            self.assertEqual(os.read(descriptor_fd, 64), b"original")
        finally:
            os.close(descriptor_fd)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "missing, replaced, or symlinked",
        ):
            MEDIA.open_sealed_backup_regular(
                backup,
                root=sealed,
                root_authority=authority,
            )

        moved.rename(sealed)
        outside = shared / "outside"
        private_directory(outside)
        sealed.rename(moved)
        sealed.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "missing, replaced, or symlinked",
        ):
            MEDIA.open_sealed_backup_regular(
                backup,
                root=sealed,
                root_authority=authority,
            )

    def test_backup_discovery_accepts_only_private_approved_formats(self) -> None:
        custom = self.backup_a / "one.dump"
        private_file(custom, b"PGDMP" + b"\0" * 700)
        tar = self.backup_b / "two.tar"
        header = bytearray(512)
        header[257:262] = b"ustar"
        private_file(tar, bytes(header))
        private_file(self.backup_b / "notes.txt", b"not a backup")

        first, first_scan = MEDIA._walk_backup_root(
            self.backup_a,
            self.policy,
            root_authority=self.backup_a_identity,
        )
        second, second_scan = MEDIA._walk_backup_root(
            self.backup_b,
            self.policy,
            root_authority=self.backup_b_identity,
        )
        self.assertEqual(first[0].backup_format, "postgres-custom-v1")
        by_id = {value.media_id: value for value in second}
        self.assertEqual(
            by_id[f"postgres-backup:{tar}"].backup_format,
            "postgres-tar-v1",
        )
        self.assertEqual(
            by_id[f"reviewed-file:{self.backup_b / 'notes.txt'}"].signature,
            "non-postgres",
        )
        self.assertEqual(len(first_scan) + len(second_scan), 3)

        (self.backup_a / "hostile.dump").symlink_to(custom)
        with self.assertRaises(OSError):
            MEDIA._walk_backup_root(
                self.backup_a,
                self.policy,
                root_authority=self.backup_a_identity,
            )

    def test_backup_roots_must_preexist_at_exact_private_mode(self) -> None:
        os.chmod(self.backup_a, 0o775)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            rf"mode-0700 directory: {re.escape(str(self.backup_a))}",
        ):
            MEDIA._walk_backup_root(
                self.backup_a,
                self.policy,
                root_authority=self.backup_a_identity,
            )
        self.assertEqual(stat.S_IMODE(self.backup_a.stat().st_mode), 0o775)

        missing = self.root / "missing-approved-root"
        missing_policy = MEDIA.DiscoveryPolicy(backup_roots=(missing,))
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            rf"mode-0700 directory: {re.escape(str(missing))}",
        ):
            MEDIA._walk_backup_root(
                missing,
                missing_policy,
                root_authority={
                    "path": str(missing),
                    "device": 0,
                    "inode": 1,
                    "uid": os.geteuid(),
                    "mode": 0o700,
                },
            )
        self.assertFalse(missing.exists())


class LedgerRulesTests(unittest.TestCase):
    @staticmethod
    def rows(
        values: tuple[tuple[str, str], ...],
    ) -> list[dict[str, str]]:
        return [
            {"version": version, "checksum": checksum}
            for version, checksum in values
        ]

    def test_contiguous_prefix_relation_alias_and_dirty_rules(self) -> None:
        prefix = MEDIA.CANONICAL_MIGRATION_LEDGER[:7]
        result = MEDIA.analyze_ledger(
            self.rows(prefix),
            legacy_relation_present=True,
            isolated=False,
        )
        self.assertEqual(result["canonical_prefix_length"], 7)

        with_alias = sorted(
            [
                *prefix,
                (
                    MEDIA.LEGACY_0005_ALIAS_VERSION,
                    MEDIA.LEGACY_0005_ALIAS_CHECKSUM,
                ),
            ]
        )
        result = MEDIA.analyze_ledger(
            self.rows(tuple(with_alias)),
            legacy_relation_present=True,
            isolated=False,
        )
        self.assertTrue(result["historical_0005_alias_present"])

        missing = (
            MEDIA.CANONICAL_MIGRATION_LEDGER[0],
            MEDIA.CANONICAL_MIGRATION_LEDGER[2],
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "contiguous prefix",
        ):
            MEDIA.analyze_ledger(
                self.rows(missing),
                legacy_relation_present=False,
                isolated=True,
            )

        dirty = [
            *MEDIA.CANONICAL_MIGRATION_LEDGER[:8],
            (
                "0009_monomer_md_job_leases",
                MEDIA.KNOWN_DIRTY_0009_CHECKSUM,
            ),
        ]
        with self.assertRaisesRegex(MEDIA.MediaEvidenceError, "not isolated"):
            MEDIA.analyze_ledger(
                self.rows(tuple(dirty)),
                legacy_relation_present=True,
                isolated=False,
            )
        result = MEDIA.analyze_ledger(
            self.rows(tuple(dirty)),
            legacy_relation_present=True,
            isolated=True,
        )
        self.assertEqual(result["status"], "known-isolated-dirty")

    def test_superseded_0013_requires_0014_without_rewriting_history(self) -> None:
        ledger = [
            *MEDIA.CANONICAL_MIGRATION_LEDGER[:12],
            (
                "0013_monomer_dft_jobs",
                MEDIA.SUPERSEDED_0013_CHECKSUM,
            ),
        ]
        result = MEDIA.analyze_ledger(
            self.rows(tuple(ledger)),
            legacy_relation_present=False,
            isolated=True,
        )
        self.assertTrue(result["requires_0014"])
        self.assertEqual(
            result["migration_0013"]["state"],
            "superseded-requires-0014",
        )

    def test_empty_ledger_is_valid_only_for_isolated_media(self) -> None:
        result = MEDIA.analyze_ledger(
            [],
            legacy_relation_present=False,
            isolated=True,
        )
        self.assertEqual(result["status"], "empty-isolated")
        with self.assertRaisesRegex(MEDIA.MediaEvidenceError, "not isolated"):
            MEDIA.analyze_ledger(
                [],
                legacy_relation_present=False,
                isolated=False,
            )


class DatabaseRelationAuthorityTests(unittest.TestCase):
    @staticmethod
    def payload(relation: dict[str, object]) -> bytes:
        migration = MEDIA.CANONICAL_MIGRATION_LEDGER[0]
        records = [
            {
                "record_type": "database",
                "database": "nexpoly",
                "current_user": "postgres",
                "transaction_read_only": True,
                "role_superuser": True,
                "role_create_db": True,
                "role_create_role": True,
                "system_identifier": "7312345678901234567",
                "database_oid": "16384",
                "database_owner": "postgres",
                "encoding": "UTF8",
                "collate": "C",
                "ctype": "C",
                "server_version_num": 160004,
                "data_directory": "/var/lib/postgresql/data",
            },
            {
                "record_type": "ledger",
                "rows": [
                    {"version": migration[0], "checksum": migration[1]}
                ],
                "relation": relation,
            },
            {
                "record_type": "legacy_relation",
                "present": False,
                "relation": None,
                "rows": [],
            },
        ]
        return b"\n".join(
            json.dumps(value, sort_keys=True).encode()
            for value in records
        ) + b"\n"

    @staticmethod
    def ledger_relation() -> dict[str, object]:
        columns = [
            {
                "number": index,
                "name": name,
                "type": data_type,
                "not_null": not_null,
                "identity": "",
                "generated": "",
                "default": default,
            }
            for index, (
                name,
                data_type,
                not_null,
                default,
            ) in enumerate(MEDIA.LEDGER_RELATION_COLUMNS, start=1)
        ]
        return {
            "oid": "17000",
            "kind": "r",
            "owner": "postgres",
            "columns": columns,
            "indexes": [
                (
                    "CREATE UNIQUE INDEX schema_migrations_pkey ON "
                    "governance.schema_migrations USING btree (version)"
                )
            ],
            "constraints": [
                {
                    "name": "schema_migrations_pkey",
                    "type": "p",
                    "definition": "PRIMARY KEY (version)",
                }
            ],
        }

    def test_ordinary_canonical_ledger_relation_is_authorized(self) -> None:
        result = MEDIA._parse_database_audit(
            self.payload(self.ledger_relation()),
            expected_database="nexpoly",
            expected_user="postgres",
            isolated=True,
        )
        self.assertEqual(
            result["ledger_relation"]["schema_sha256"],
            MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(
                    result["ledger_relation"]["schema_authority"]
                )
            ),
        )

    def test_view_foreign_owner_and_altered_columns_are_rejected(self) -> None:
        for name, mutate in (
            (
                "view",
                lambda relation: relation.update({"kind": "v"}),
            ),
            (
                "foreign-owner",
                lambda relation: relation.update({"owner": "attacker"}),
            ),
            (
                "altered-column",
                lambda relation: relation["columns"][1].update(
                    {"type": "character varying"}
                ),
            ),
        ):
            with self.subTest(name=name):
                relation = self.ledger_relation()
                mutate(relation)
                with self.assertRaisesRegex(
                    MEDIA.MediaEvidenceError,
                    "relation|ledger",
                ):
                    MEDIA._parse_database_audit(
                        self.payload(relation),
                        expected_database="nexpoly",
                        expected_user="postgres",
                        isolated=True,
                    )


def database_sql_payload(
    *,
    database: str,
    user: str,
    oid: str,
    owner: str,
    data_directory: str = "/var/lib/postgresql/data",
    ledger: list[dict[str, str]] | None = None,
    role_superuser: bool = False,
) -> bytes:
    rows = (
        [
            {
                "version": MEDIA.CANONICAL_MIGRATION_LEDGER[0][0],
                "checksum": MEDIA.CANONICAL_MIGRATION_LEDGER[0][1],
            }
        ]
        if ledger is None
        else ledger
    )
    relation = DatabaseRelationAuthorityTests.ledger_relation()
    relation["owner"] = owner
    records = [
        {
            "record_type": "database",
            "database": database,
            "current_user": user,
            "transaction_read_only": True,
            "role_superuser": role_superuser,
            "role_create_db": role_superuser,
            "role_create_role": role_superuser,
            "system_identifier": "7312345678901234567",
            "database_oid": oid,
            "database_owner": owner,
            "encoding": "UTF8",
            "collate": "C",
            "ctype": "C",
            "server_version_num": 160004,
            "data_directory": data_directory,
        },
        {
            "record_type": "ledger",
            "rows": rows,
            "relation": relation,
        },
        {
            "record_type": "legacy_relation",
            "present": False,
            "relation": None,
            "rows": [],
        },
    ]
    return b"\n".join(
        json.dumps(value, sort_keys=True).encode("utf-8")
        for value in records
    ) + b"\n"


class DatabaseInventoryV3Tests(unittest.TestCase):
    @staticmethod
    def authority(
        name: str,
        oid: str,
        *,
        migration_scope: str = "nexpoly-ledger",
    ) -> dict[str, object]:
        return {
            "name": name,
            "oid": oid,
            "owner": "postgres",
            "allow_connections": True,
            "template": False,
            "audit_role": "auditor",
            "migration_scope": migration_scope,
        }

    def test_full_inventory_audits_every_declared_database_and_rejects_hidden(
        self,
    ) -> None:
        authorities = (
            self.authority("nexpoly", "16384"),
            self.authority("nexpoly_shadow", "16385"),
        )
        current = MEDIA.MediaDescriptor(
            media_id="docker-volume:fixture",
            kind="docker_volume",
            database="nexpoly",
            database_user="auditor",
            disposition="read-only-online",
            audit_method="live-read-only",
            pg_service="audit",
            databases=authorities,
        )
        inventory = [
            {
                key: authority[key]
                for key in (
                    "name",
                    "oid",
                    "owner",
                    "allow_connections",
                    "template",
                )
            }
            for authority in authorities
        ]

        def audit(_runner, _container, _descriptor, **kwargs):
            authority = kwargs["database_authority"]
            value = database_audit(
                str(authority["name"]),
                "auditor",
                system_identifier="7312345678901234567",
                database_oid=str(authority["oid"]),
                database_owner="postgres",
            )
            value.pop("_unused_empty_digest")
            if authority["name"] == "nexpoly_shadow":
                value["migration_0013"] = {
                    "state": "superseded-requires-0014",
                    "checksum": MEDIA.SUPERSEDED_0013_CHECKSUM,
                }
                value["requires_0014"] = True
            return value

        with (
            mock.patch.object(
                MEDIA,
                "_container_database_inventory",
                return_value=inventory,
            ),
            mock.patch.object(
                MEDIA,
                "_audit_container_database",
                side_effect=audit,
            ) as audited,
        ):
            bundle = MEDIA._audit_container_medium(
                MEDIA.CommandRunner(),
                CONTAINER_A,
                current,
                isolated=False,
                expected_data_directory="/var/lib/postgresql/data",
            )
        self.assertEqual(
            [record["name"] for record in bundle["databases"]],
            ["nexpoly", "nexpoly_shadow"],
        )
        self.assertEqual(audited.call_count, 2)
        self.assertTrue(
            bundle["databases"][1]["audit"]["requires_0014"]
        )

        incomplete = MEDIA.MediaDescriptor(
            **{
                **current.document(),
                "databases": (authorities[0],),
            }
        )
        with (
            mock.patch.object(
                MEDIA,
                "_container_database_inventory",
                return_value=inventory,
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "complete database inventory differs",
            ),
        ):
            MEDIA._audit_container_medium(
                MEDIA.CommandRunner(),
                CONTAINER_A,
                incomplete,
                isolated=False,
                expected_data_directory="/var/lib/postgresql/data",
            )

    def test_live_audit_uses_only_exact_container_unix_socket(self) -> None:
        authority = self.authority("nexpoly", "16384")
        current = MEDIA.MediaDescriptor(
            media_id="docker-volume:fixture",
            kind="docker_volume",
            database="nexpoly",
            database_user="auditor",
            disposition="read-only-online",
            audit_method="live-read-only",
            pg_service="must-not-be-used",
            databases=(authority,),
        )
        class ExactRunner(MEDIA.CommandRunner):
            def __init__(self) -> None:
                self.commands: list[tuple[list[str], bytes | None]] = []
                self.mount = {
                    "Type": "volume",
                    "Name": "fixture",
                    "Source": "fixture",
                    "Destination": "/var/lib/postgresql/data",
                    "RW": True,
                }
                self.container = {
                    "Id": CONTAINER_A,
                    "Name": "/fixture",
                    "Image": "sha256:" + "4" * 64,
                    "Created": "2026-07-17T10:00:00Z",
                    "Path": "postgres",
                    "Args": [],
                    "RestartCount": 0,
                    "Config": {},
                    "HostConfig": {},
                    "State": {
                        "Status": "running",
                        "StartedAt": "2026-07-17T10:01:00Z",
                        "FinishedAt": "0001-01-01T00:00:00Z",
                    },
                    "Mounts": [self.mount],
                }

            def run(self, arguments, *, input_bytes=None, **_kwargs):
                values = list(arguments)
                self.commands.append((values, input_bytes))
                if values[1:4] == ["ps", "-aq", "--no-trunc"]:
                    return subprocess.CompletedProcess(
                        values, 0, stdout=(CONTAINER_A + "\n").encode()
                    )
                if values[1:3] == ["container", "inspect"]:
                    return subprocess.CompletedProcess(
                        values,
                        0,
                        stdout=json.dumps([self.container]).encode(),
                    )
                if values[1] == "exec" and "psql" in values:
                    if input_bytes == MEDIA.DATABASE_INVENTORY_SQL.encode():
                        payload = {
                            "record_type": "database_inventory",
                            "databases": [
                                {
                                    key: authority[key]
                                    for key in (
                                        "name",
                                        "oid",
                                        "owner",
                                        "allow_connections",
                                        "template",
                                    )
                                }
                            ],
                        }
                        stdout = json.dumps(payload).encode() + b"\n"
                    else:
                        stdout = database_sql_payload(
                            database="nexpoly",
                            user="auditor",
                            oid="16384",
                            owner="postgres",
                        )
                    return subprocess.CompletedProcess(
                        values, 0, stdout=stdout
                    )
                raise AssertionError(values)

        runner = ExactRunner()
        source = MEDIA.DiscoveredMedia(
            media_id=current.media_id,
            kind="docker_volume",
            locator="fixture",
            data_subpath=".",
            attached=(
                MEDIA._attached_record(runner.container, runner.mount),
            ),
        )
        bundle = MEDIA._run_live_audit(runner, current, source)
        self.assertEqual(bundle["database_identity"]["database"], "nexpoly")
        psql_commands = [
            values
            for values, _payload in runner.commands
            if values[1] == "exec" and "psql" in values
        ]
        self.assertEqual(len(psql_commands), 2)
        for command in psql_commands:
            self.assertEqual(command[5], CONTAINER_A)
            self.assertIn("/var/run/postgresql", command)
            self.assertNotIn("must-not-be-used", command)
            self.assertNotIn("127.0.0.1", command)


class RecordOnlyMediaV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="postgres-record-only-v3-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.workspace = self.root / "workspace"
        private_directory(self.workspace)
        self.registry = MEDIA.Registry(
            payload=b"v3",
            digest=MEDIA.sha256_bytes(b"v3"),
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            service_file_sha256="sha256:" + "7" * 64,
            descriptors=(),
            required_online_databases=(),
            boundary={},
        )
        self.operation = PassthroughScratchOperation(
            MEDIA.CommandRunner(),
            self.workspace,
        )

    def test_reviewed_non_pg_file_is_content_sealed(self) -> None:
        path = self.root / "reviewed.txt"
        private_file(path, b"reviewed non-pg\n")
        media_id = f"reviewed-file:{path}"
        current = MEDIA.MediaDescriptor(
            media_id=media_id,
            kind="reviewed_file",
            database="none",
            database_user="none",
            disposition="excluded-from-nexpoly-migration",
            audit_method="reviewed-content-only",
            pg_service=None,
            classification="reviewed-non-pg",
            source_postgres_major=None,
        )
        source = MEDIA.DiscoveredMedia(
            media_id=media_id,
            kind="reviewed_file",
            locator=str(path),
            data_subpath=".",
            attached=(),
            signature="non-postgres",
            postgres_major=None,
        )
        policy = MEDIA.DiscoveryPolicy(backup_roots=(self.root,))
        registry = MEDIA.Registry(
            payload=self.registry.payload,
            digest=self.registry.digest,
            audit_image=self.registry.audit_image,
            auditor_sha256=self.registry.auditor_sha256,
            service_file_sha256=self.registry.service_file_sha256,
            descriptors=(),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(policy),
        )
        record = MEDIA._record_only_medium(
            MEDIA.CommandRunner(),
            registry,
            current,
            source,
            self.workspace,
            policy=policy,
            operation=self.operation,
            resource_prefix="medium-record",
            auditor_sha256="sha256:" + "6" * 64,
            audit_image_id="sha256:" + "5" * 64,
            service_file_sha256="sha256:" + "7" * 64,
            audited_at="2026-07-17T12:00:00Z",
        )
        validated, _runtime = (
            CONTRACTS._external_record_only_medium_v3(
                record,
                volume_names=[],
                container_ids=[],
            )
        )
        self.assertEqual(validated["record_type"], "reviewed-non-pg")

    def test_adjacent_pg18_volume_is_never_started_as_postgres(self) -> None:
        media_id = "docker-volume:adjacent-pg18"
        current = MEDIA.MediaDescriptor(
            media_id=media_id,
            kind="docker_volume",
            database="none",
            database_user="none",
            disposition="excluded-from-nexpoly-migration",
            audit_method="adjacent-record-only",
            pg_service=None,
            classification="adjacent-record-only",
            source_postgres_major=18,
        )
        source = MEDIA.DiscoveredMedia(
            media_id=media_id,
            kind="docker_volume",
            locator="adjacent-pg18",
            data_subpath=".",
            attached=(),
            signature="postgres",
            postgres_major=18,
        )
        identity = {
            "name": "adjacent-pg18",
            "driver": "local",
            "mountpoint": (
                "/var/lib/docker/volumes/adjacent-pg18/_data"
            ),
            "labels_sha256": "sha256:" + "1" * 64,
            "inspect_sha256": "sha256:" + "2" * 64,
            "data_subpath": ".",
            "attached": [],
        }
        with (
            mock.patch.object(
                MEDIA,
                "_docker_volume_identity",
                return_value=identity,
            ),
            mock.patch.object(
                MEDIA,
                "_volume_content_digest",
                return_value="sha256:" + "3" * 64,
            ),
        ):
            record = MEDIA._record_only_medium(
                MEDIA.CommandRunner(),
                self.registry,
                current,
                source,
                self.workspace,
                policy=MEDIA.DiscoveryPolicy(backup_roots=(self.root,)),
                operation=self.operation,
                resource_prefix="medium-record",
                auditor_sha256="sha256:" + "6" * 64,
                audit_image_id="sha256:" + "5" * 64,
                service_file_sha256="sha256:" + "7" * 64,
                audited_at="2026-07-17T12:00:00Z",
            )
        self.assertFalse(
            record["audit"]["isolation"]["source_started_as_postgres"]
        )
        CONTRACTS._external_record_only_medium_v3(
            record,
            volume_names=["adjacent-pg18"],
            container_ids=[],
        )

    def test_adjacent_system_backup_is_content_sealed_without_restore(
        self,
    ) -> None:
        backups = self.root / "backups"
        private_directory(backups)
        path = backups / "postgres.dump"
        private_file(path, b"PGDMP" + b"\0" * 1024)
        media_id = f"postgres-backup:{path}"
        current = MEDIA.MediaDescriptor(
            media_id=media_id,
            kind="postgres_backup",
            database="none",
            database_user="none",
            disposition="excluded-from-nexpoly-migration",
            audit_method="adjacent-record-only",
            pg_service=None,
            classification="adjacent-record-only",
            source_postgres_major=None,
        )
        source = MEDIA.DiscoveredMedia(
            media_id=media_id,
            kind="postgres_backup",
            locator=str(path),
            data_subpath=".",
            attached=(),
            backup_format="postgres-custom-v1",
            signature="postgres-backup",
            postgres_major=None,
        )
        policy = MEDIA.DiscoveryPolicy(backup_roots=(backups,))
        registry = MEDIA.Registry(
            payload=self.registry.payload,
            digest=self.registry.digest,
            audit_image=self.registry.audit_image,
            auditor_sha256=self.registry.auditor_sha256,
            service_file_sha256=self.registry.service_file_sha256,
            descriptors=(),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(policy),
        )
        record = MEDIA._record_only_medium(
            MEDIA.CommandRunner(),
            registry,
            current,
            source,
            self.workspace,
            policy=policy,
            operation=self.operation,
            resource_prefix="medium-record",
            auditor_sha256="sha256:" + "6" * 64,
            audit_image_id="sha256:" + "5" * 64,
            service_file_sha256="sha256:" + "7" * 64,
            audited_at="2026-07-17T12:00:00Z",
        )
        validated, _runtime = (
            CONTRACTS._external_record_only_medium_v3(
                record,
                volume_names=[],
                container_ids=[],
            )
        )
        self.assertEqual(
            validated["source_identity_before"]["format"],
            "postgres-custom-v1",
        )
        self.assertEqual(
            validated["source_content_sha256"],
            validated["source_identity_before"]["sha256"],
        )
        self.assertEqual(
            validated["audit"]["isolation"],
            {
                "source_opened_with_openat_no_follow": True,
                "source_passed_to_docker": False,
                "source_started_as_postgres": False,
                "content_cas_verified": True,
            },
        )

        tampered = copy.deepcopy(record)
        tampered["source_content_sha256"] = "sha256:" + "9" * 64
        with self.assertRaisesRegex(
            CONTRACTS.SiteHelperContractError,
            "content digest differs",
        ):
            CONTRACTS._external_record_only_medium_v3(
                tampered,
                volume_names=[],
                container_ids=[],
            )

        os.chmod(path, 0o640)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "private backup file is unsafe",
        ):
            MEDIA._record_only_medium(
                MEDIA.CommandRunner(),
                registry,
                current,
                source,
                self.workspace,
                policy=policy,
                operation=self.operation,
                resource_prefix="medium-record-unsafe",
                auditor_sha256="sha256:" + "6" * 64,
                audit_image_id="sha256:" + "5" * 64,
                service_file_sha256="sha256:" + "7" * 64,
                audited_at="2026-07-17T12:00:00Z",
            )

    def test_adjacent_backup_file_replacement_race_fails_content_cas(
        self,
    ) -> None:
        backups = self.root / "race-backups"
        private_directory(backups)
        path = backups / "postgres.dump"
        private_file(path, b"PGDMP original")
        media_id = f"postgres-backup:{path}"
        current = MEDIA.MediaDescriptor(
            media_id=media_id,
            kind="postgres_backup",
            database="none",
            database_user="none",
            disposition="excluded-from-nexpoly-migration",
            audit_method="adjacent-record-only",
            pg_service=None,
            classification="adjacent-record-only",
            source_postgres_major=None,
        )
        source = MEDIA.DiscoveredMedia(
            media_id=media_id,
            kind="postgres_backup",
            locator=str(path),
            data_subpath=".",
            attached=(),
            backup_format="postgres-custom-v1",
            signature="postgres-backup",
            postgres_major=None,
        )
        policy = MEDIA.DiscoveryPolicy(backup_roots=(backups,))
        registry = MEDIA.Registry(
            payload=self.registry.payload,
            digest=self.registry.digest,
            audit_image=self.registry.audit_image,
            auditor_sha256=self.registry.auditor_sha256,
            service_file_sha256=self.registry.service_file_sha256,
            descriptors=(),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(policy),
        )
        original_open = MEDIA.open_sealed_backup_regular
        calls = 0

        def open_then_replace(*args, **kwargs):
            nonlocal calls
            descriptor_fd = original_open(*args, **kwargs)
            calls += 1
            if calls == 1:
                path.rename(backups / "postgres.dump.old")
                private_file(path, b"PGDMP replacement")
            return descriptor_fd

        with (
            mock.patch.object(
                MEDIA,
                "open_sealed_backup_regular",
                side_effect=open_then_replace,
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "changed during content audit",
            ),
        ):
            MEDIA._record_only_medium(
                MEDIA.CommandRunner(),
                registry,
                current,
                source,
                self.workspace,
                policy=policy,
                operation=self.operation,
                resource_prefix="medium-record-race",
                auditor_sha256="sha256:" + "6" * 64,
                audit_image_id="sha256:" + "5" * 64,
                service_file_sha256="sha256:" + "7" * 64,
                audited_at="2026-07-17T12:00:00Z",
            )


class FakeDockerRunner(MEDIA.CommandRunner):
    def __init__(
        self,
        containers: list[dict[str, object]],
        volumes: list[dict[str, object]],
        probe: dict[str, str],
    ) -> None:
        self.containers = {value["Id"]: value for value in containers}
        self.volumes = {value["Name"]: value for value in volumes}
        self.probe = probe
        self.probed: list[str] = []

    @staticmethod
    def complete(
        arguments,
        stdout: bytes = b"",
        *,
        returncode: int = 0,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            arguments,
            returncode,
            stdout=stdout,
            stderr=b"",
        )

    def run(
        self,
        arguments,
        *,
        input_bytes=None,
        timeout=120,
        check=True,
        env=None,
    ):
        del input_bytes, timeout, check, env
        values = list(arguments)
        if values[1:4] == ["ps", "-aq", "--no-trunc"]:
            return self.complete(
                values,
                ("\n".join(sorted(self.containers)) + "\n").encode(),
            )
        if values[1:3] == ["container", "inspect"]:
            identifier = values[-1]
            return self.complete(
                values,
                json.dumps([self.containers[identifier]]).encode(),
            )
        if values[1:4] == ["volume", "ls", "--format"]:
            return self.complete(
                values,
                ("\n".join(sorted(self.volumes)) + "\n").encode(),
            )
        if values[1:3] == ["volume", "inspect"]:
            name = values[-1]
            return self.complete(
                values,
                json.dumps([self.volumes[name]]).encode(),
            )
        if values[1] == "run":
            mount = next(
                value
                for value in values
                if isinstance(value, str)
                and value.startswith("type=volume,src=")
            )
            name = mount.split(",")[1].split("=", 1)[1]
            self.probed.append(name)
            return self.complete(values, self.probe.get(name, "").encode())
        raise AssertionError(f"unexpected fake Docker command: {values!r}")


class PassthroughScratchOperation:
    """Small orchestration seam for tests unrelated to journal durability."""

    def __init__(self, runner, workspace: Path) -> None:
        self.runner = runner
        self.workspace = workspace
        self.journal: dict[str, object] = {"resources": []}
        self.resources: dict[str, tuple[str, str]] = {}

    def run_container(
        self,
        resource_key,
        arguments,
        **_kwargs,
    ):
        completed = self.runner.run(arguments)
        if "-d" in arguments:
            identifier = completed.stdout.decode("ascii").strip()
            self.resources[resource_key] = ("container", identifier)
            self.journal["resources"].append(
                {"resource_key": resource_key}
            )
        return completed

    def create_volume(self, resource_key, **_kwargs):
        name = MEDIA._temp_name("fixture")
        self.runner.run(
            [MEDIA.DOCKER, "volume", "create", "--", name]
        )
        self.resources[resource_key] = ("volume", name)
        self.journal["resources"].append({"resource_key": resource_key})
        return name

    def remove_resource(self, resource_key):
        kind, identity = self.resources.pop(resource_key)
        if kind == "container":
            self.runner.run(
                [
                    MEDIA.DOCKER,
                    "container",
                    "rm",
                    "-f",
                    "--",
                    identity,
                ],
                check=False,
            )
            self.runner.run(
                [MEDIA.DOCKER, "container", "inspect", "--", identity],
                check=False,
            )
        else:
            self.runner.run(
                [MEDIA.DOCKER, "volume", "rm", "--", identity],
                check=False,
            )
            self.runner.run(
                [MEDIA.DOCKER, "volume", "inspect", "--", identity],
                check=False,
            )


class StatefulScratchDockerRunner(MEDIA.CommandRunner):
    """In-memory Docker identity model for scratch ownership tests."""

    def __init__(self) -> None:
        self.image_id = "sha256:" + "5" * 64
        self.volumes: dict[str, dict[str, object]] = {}
        self.containers: dict[str, dict[str, object]] = {}
        self.calls: list[list[str]] = []
        self.events: list[tuple[str, str]] = []
        self.next_container = 10
        self.fail_volume_create_before_create = False
        self.fail_volume_create_after_create = False
        self.fail_run_before_create = False
        self.fail_run_after_create = False
        self.detached_response_id: str | None = None

    @staticmethod
    def _labels(arguments: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for index, value in enumerate(arguments[:-1]):
            if value == "--label":
                key, item = arguments[index + 1].split("=", 1)
                result[key] = item
        return result

    @staticmethod
    def _mounts(arguments: list[str]) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for index, value in enumerate(arguments[:-1]):
            if value == "--mount":
                fields: dict[str, str | bool] = {}
                for part in arguments[index + 1].split(","):
                    if "=" in part:
                        key, item = part.split("=", 1)
                        fields[key] = item
                    else:
                        fields[part] = True
                kind = str(fields["type"])
                result.append(
                    {
                        "Type": kind,
                        "Name": (
                            fields.get("src") if kind == "volume" else None
                        ),
                        "Source": (
                            fields.get("src") if kind == "bind" else ""
                        ),
                        "Destination": fields["dst"],
                        "RW": "readonly" not in fields,
                    }
                )
        return result

    @staticmethod
    def _tmpfs(arguments: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for index, value in enumerate(arguments[:-1]):
            if value == "--tmpfs":
                destination, options = arguments[index + 1].split(":", 1)
                result[destination] = options
        return result

    def materialize_volume(
        self,
        name: str,
        labels: dict[str, str],
        *,
        driver: str = "local",
    ) -> dict[str, object]:
        value = {
            "Name": name,
            "Driver": driver,
            "Mountpoint": f"/var/lib/docker/volumes/{name}/_data",
            "Labels": dict(labels),
            "Options": None,
            "Scope": "local",
        }
        self.volumes[name] = value
        return value

    def materialize_container(
        self,
        name: str,
        labels: dict[str, str],
        *,
        mounts: list[dict[str, object]],
        image: str = IMAGE,
        image_id: str | None = None,
        read_only: bool = True,
        command: list[str] | None = None,
        entrypoint: str | None = None,
        environment: list[str] | None = None,
        tmpfs: dict[str, str] | None = None,
    ) -> dict[str, object]:
        identifier = f"{self.next_container:064x}"
        self.next_container += 1
        value = {
            "Id": identifier,
            "Name": f"/{name}",
            "Image": image_id or self.image_id,
            "Created": "2026-07-18T00:00:00.000000000Z",
            "Path": "fixture",
            "Args": [],
            "Config": {
                "Image": image,
                "Labels": dict(labels),
                "Env": list(environment or []),
                "Entrypoint": (
                    [entrypoint] if entrypoint is not None else None
                ),
                "Cmd": list(command or []),
            },
            "HostConfig": {
                "NetworkMode": "none",
                "ReadonlyRootfs": read_only,
                "Privileged": False,
                "RestartPolicy": {"Name": "no"},
                "PortBindings": {},
                "Devices": [],
                "Tmpfs": dict(tmpfs or {}),
            },
            "Mounts": copy.deepcopy(mounts),
        }
        self.containers[identifier] = value
        return value

    def _container_by_name(self, identity: str):
        normalized = identity.removeprefix("/")
        for value in self.containers.values():
            if value["Name"] == f"/{normalized}" or value["Id"] == identity:
                return value
        return None

    def run(
        self,
        arguments,
        *,
        input_bytes=None,
        timeout=120,
        check=True,
        env=None,
    ):
        del input_bytes, timeout, check, env
        values = list(arguments)
        self.calls.append(values)
        if values[1:3] == ["image", "inspect"]:
            return FakeDockerRunner.complete(
                values,
                json.dumps(
                    [
                        {
                            "Id": self.image_id,
                            "RepoDigests": [IMAGE],
                        }
                    ]
                ).encode(),
            )
        if values[1:3] == ["volume", "create"]:
            name = values[-1]
            if self.fail_volume_create_before_create:
                self.fail_volume_create_before_create = False
                raise subprocess.TimeoutExpired(values, 1)
            if name not in self.volumes:
                self.materialize_volume(name, self._labels(values))
            if self.fail_volume_create_after_create:
                self.fail_volume_create_after_create = False
                raise subprocess.TimeoutExpired(values, 1)
            return FakeDockerRunner.complete(values, (name + "\n").encode())
        if values[1:4] == ["volume", "ls", "--format"]:
            return FakeDockerRunner.complete(
                values,
                (
                    "\n".join(sorted(self.volumes))
                    + ("\n" if self.volumes else "")
                ).encode(),
            )
        if values[1:3] == ["volume", "inspect"]:
            name = values[-1]
            if name not in self.volumes:
                return subprocess.CompletedProcess(
                    values,
                    1,
                    stdout=b"",
                    stderr=b"Error: no such volume",
                )
            return FakeDockerRunner.complete(
                values,
                json.dumps([self.volumes[name]]).encode(),
            )
        if values[1:3] == ["volume", "rm"]:
            name = values[-1]
            self.events.append(("volume-rm", name))
            self.volumes.pop(name, None)
            return FakeDockerRunner.complete(values)
        if values[1:4] == ["ps", "-aq", "--no-trunc"]:
            return FakeDockerRunner.complete(
                values,
                (
                    "\n".join(sorted(self.containers))
                    + ("\n" if self.containers else "")
                ).encode(),
            )
        if values[1:3] == ["container", "inspect"]:
            value = self._container_by_name(values[-1])
            if value is None:
                return subprocess.CompletedProcess(
                    values,
                    1,
                    stdout=b"",
                    stderr=b"Error: No such container",
                )
            return FakeDockerRunner.complete(
                values,
                json.dumps([value]).encode(),
            )
        if values[1:3] == ["container", "rm"]:
            identity = values[-1]
            value = self._container_by_name(identity)
            self.events.append(("container-rm", identity))
            if value is not None:
                self.containers.pop(str(value["Id"]), None)
            return FakeDockerRunner.complete(values)
        if values[1] == "run":
            if self.fail_run_before_create:
                self.fail_run_before_create = False
                raise subprocess.TimeoutExpired(values, 1)
            name = values[values.index("--name") + 1]
            read_only = "--read-only" in values
            labels = self._labels(values)
            image_index = values.index(IMAGE)
            entrypoint = (
                values[values.index("--entrypoint") + 1]
                if "--entrypoint" in values[:image_index]
                else None
            )
            environment = [
                values[index + 1]
                for index, value in enumerate(values[:image_index])
                if value == "--env"
            ]
            container = self.materialize_container(
                name,
                labels,
                mounts=self._mounts(values),
                read_only=read_only,
                command=values[image_index + 1 :],
                entrypoint=entrypoint,
                environment=environment,
                tmpfs=self._tmpfs(values),
            )
            if self.fail_run_after_create:
                self.fail_run_after_create = False
                raise subprocess.TimeoutExpired(values, 1)
            resource_key = labels["io.nexpoly.audit.resource"]
            if "-d" in values:
                output = (
                    self.detached_response_id or str(container["Id"])
                ) + "\n"
            elif "digest" in resource_key:
                output = "a" * 64 + "  -\n"
            elif resource_key.endswith("-version"):
                output = "postgres (PostgreSQL) 16.4\n"
            elif resource_key.endswith("-postgres-user"):
                output = "70:70\n"
            elif resource_key.endswith("-probe"):
                output = postgres_signature("/source")
            else:
                output = ""
            return FakeDockerRunner.complete(
                values,
                output.encode(),
            )
        if values[1] == "exec":
            return FakeDockerRunner.complete(values)
        raise AssertionError(f"unexpected scratch Docker command: {values!r}")


class CompleteDockerDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="postgres-media-discovery-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.backups = self.root / "backups"
        private_directory(self.backups)
        self.policy = MEDIA.DiscoveryPolicy(backup_roots=(self.backups,))

    @staticmethod
    def volume(name: str) -> dict[str, object]:
        return {
            "Name": name,
            "Driver": "local",
            "Mountpoint": f"/var/lib/docker/volumes/{name}/_data",
            "Labels": {},
            "Options": None,
            "Scope": "local",
        }

    def test_volume_probe_ignores_per_database_pg_version_files(self) -> None:
        name = "postgres16-with-database-version-files"
        runner = FakeDockerRunner(
            [],
            [self.volume(name)],
            {
                name: (
                    postgres_signature("/source")
                    + "V\t/source/base/1/PG_VERSION\t16\n"
                    + "V\t/source/base/16384/PG_VERSION\t16\n"
                    + "V\t/source/base/5/PG_VERSION\t16\n"
                )
            },
        )

        result = MEDIA._probe_volume_signature(
            runner,
            IMAGE,
            name,
            operation=PassthroughScratchOperation(runner, self.root),
            resource_key="postgres16-signature-probe",
        )

        self.assertEqual(
            result,
            {
                "signature": "postgres",
                "data_subpath": ".",
                "postgres_major": 16,
            },
        )

    def test_volume_probe_still_rejects_a_second_cluster_root(self) -> None:
        name = "multiple-postgres-clusters"
        runner = FakeDockerRunner(
            [],
            [self.volume(name)],
            {
                name: (
                    postgres_signature("/source")
                    + postgres_signature("/source/other")
                )
            },
        )

        result = MEDIA._probe_volume_signature(
            runner,
            IMAGE,
            name,
            operation=PassthroughScratchOperation(runner, self.root),
            resource_key="multiple-cluster-signature-probe",
        )

        self.assertEqual(result["signature"], "damaged-postgres")

    def test_unregistered_backup_in_any_approved_root_fails_closed(self) -> None:
        unexpected = self.backups / "unexpected.dump"
        private_file(unexpected, b"PGDMP" + b"\0" * 1024)
        runner = FakeDockerRunner([], [], {})
        registry = MEDIA.Registry(
            payload=b"fixture",
            digest=MEDIA.sha256_bytes(b"fixture"),
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            service_file_sha256="sha256:" + "7" * 64,
            descriptors=(),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(self.policy),
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "additional=.*unexpected.dump",
        ):
            MEDIA.discover_media(
                registry,
                runner=runner,
                operation=PassthroughScratchOperation(runner, self.root),
                policy=self.policy,
            )

    @staticmethod
    def container(
        identifier: str,
        *,
        database_mount: dict[str, object],
        pgdata: str,
    ) -> dict[str, object]:
        return {
            "Id": identifier,
            "Name": f"/fixture-{identifier[:12]}",
            "Image": "sha256:" + "4" * 64,
            "Created": "2026-07-17T10:00:00.000000000Z",
            "Path": "docker-entrypoint.sh",
            "Args": ["postgres"],
            "RestartCount": 0,
            "Config": {
                "Image": "postgres:16",
                "Env": [f"PGDATA={pgdata}"],
                "Labels": {},
            },
            "HostConfig": {},
            "State": {
                "Status": "running",
                "StartedAt": "2026-07-17T10:01:00.000000000Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
            },
            "Mounts": [database_mount],
        }

    def test_arbitrary_volume_names_and_container_bind_are_discovered(self) -> None:
        bind = self.root / "arbitrary-bind"
        private_directory(bind)
        private_directory(bind / "global")
        private_directory(bind / "base")
        private_file(bind / "PG_VERSION", b"16\n")
        private_file(bind / "global/pg_control", b"fixture-control")
        production = "totally-unrelated-production-name"
        development = "x7"
        dormant = "old-cluster-random"
        unrelated = "cache-without-prefix"
        containers = [
            self.container(
                CONTAINER_A,
                database_mount={
                    "Type": "volume",
                    "Name": production,
                    "Source": production,
                    "Destination": "/srv/db",
                    "RW": True,
                },
                pgdata="/srv/db/pgdata",
            ),
            self.container(
                CONTAINER_B,
                database_mount={
                    "Type": "volume",
                    "Name": development,
                    "Source": development,
                    "Destination": "/var/lib/postgresql/data",
                    "RW": True,
                },
                pgdata="/var/lib/postgresql/data",
            ),
            self.container(
                CONTAINER_C,
                database_mount={
                    "Type": "bind",
                    "Source": str(bind),
                    "Destination": "/var/lib/postgresql/data",
                    "RW": True,
                },
                pgdata="/var/lib/postgresql/data",
            ),
        ]
        volumes = [
            self.volume(name)
            for name in (production, development, dormant, unrelated)
        ]
        runner = FakeDockerRunner(
            containers,
            volumes,
            {
                production: postgres_signature("/source/pgdata"),
                development: postgres_signature("/source"),
                dormant: postgres_signature("/source/pgdata"),
                unrelated: "",
            },
        )
        identifiers = sorted(
            [
                f"container-bind:{bind}",
                f"docker-volume:{development}",
                f"docker-volume:{dormant}",
                f"docker-volume:{production}",
                f"docker-volume:{unrelated}",
            ]
        )
        descriptor_map = {
            f"docker-volume:{production}": descriptor(
                f"docker-volume:{production}",
                "nexpoly",
                disposition="writable-target",
                user="production_auditor",
                service="production_audit",
            ),
            f"docker-volume:{development}": descriptor(
                f"docker-volume:{development}",
                "nexpoly_dev",
                user="dev_auditor",
                service="dev_audit",
            ),
            f"container-bind:{bind}": descriptor(
                f"container-bind:{bind}",
                "nexpoly_md_health_opt",
                user="health_auditor",
                service="health_audit",
            ),
            f"docker-volume:{dormant}": descriptor(
                f"docker-volume:{dormant}",
                "nexpoly",
                disposition="retained-private-isolated",
                method="isolated-volume-copy-read-only",
                user="postgres",
                service=None,
            ),
            f"docker-volume:{unrelated}": {
                **descriptor(
                    f"docker-volume:{unrelated}",
                    "none",
                    disposition="excluded-from-nexpoly-migration",
                    method="reviewed-content-only",
                    user="none",
                    service=None,
                    classification="reviewed-non-pg",
                    source_postgres_major=None,
                ),
                "databases": [],
            },
        }
        registry = MEDIA.Registry(
            payload=b"fixture",
            digest=MEDIA.sha256_bytes(b"fixture"),
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            service_file_sha256="sha256:" + "7" * 64,
            descriptors=tuple(
                MEDIA.MediaDescriptor(**descriptor_map[name])
                for name in identifiers
            ),
            required_online_databases=(
                {
                    "stack": "nexpoly_dev",
                    "media_id": f"docker-volume:{development}",
                },
                {
                    "stack": "nexpoly_md_health_opt",
                    "media_id": f"container-bind:{bind}",
                },
            ),
            boundary=MEDIA.seal_discovery_boundary(self.policy),
        )

        result = MEDIA.discover_media(
            registry,
            runner=runner,
            operation=PassthroughScratchOperation(runner, self.root),
            policy=self.policy,
        )

        self.assertEqual(sorted(result.media), identifiers)
        self.assertEqual(
            result.media[f"docker-volume:{dormant}"].data_subpath,
            "pgdata",
        )
        self.assertIn(unrelated, result.scanned_volume_names)
        self.assertEqual(
            result.media[f"docker-volume:{unrelated}"].signature,
            "non-postgres",
        )
        self.assertEqual(set(runner.probed), set(runner.volumes))

    def test_inactive_empty_pgdata_volume_requires_explicit_non_pg_review(
        self,
    ) -> None:
        name = "retired-empty-pgdata"
        mount = {
            "Type": "volume",
            "Name": name,
            "Source": name,
            "Destination": "/var/lib/postgresql",
            "RW": True,
        }
        container = self.container(
            CONTAINER_A,
            database_mount=mount,
            pgdata="/var/lib/postgresql/18/docker",
        )
        container["State"].update(
            {
                "Status": "exited",
                "FinishedAt": "2026-07-17T11:00:00.000000000Z",
            }
        )
        media_id = f"docker-volume:{name}"
        reviewed = {
            **descriptor(
                media_id,
                "none",
                disposition="excluded-from-nexpoly-migration",
                method="reviewed-content-only",
                user="none",
                service=None,
                classification="reviewed-non-pg",
                source_postgres_major=None,
            ),
            "databases": [],
        }
        registry = MEDIA.Registry(
            payload=b"fixture",
            digest=MEDIA.sha256_bytes(b"fixture"),
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            service_file_sha256="sha256:" + "7" * 64,
            descriptors=(MEDIA.MediaDescriptor(**reviewed),),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(self.policy),
        )

        runner = FakeDockerRunner(
            [container],
            [self.volume(name)],
            {name: ""},
        )
        result = MEDIA.discover_media(
            registry,
            runner=runner,
            operation=PassthroughScratchOperation(runner, self.root),
            policy=self.policy,
        )
        self.assertEqual(result.media[media_id].signature, "non-postgres")
        self.assertEqual(
            result.media[media_id].attached[0]["container_id"],
            CONTAINER_A,
        )

        container["State"]["Status"] = "running"
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "PGDATA conflicts",
        ):
            runner = FakeDockerRunner(
                [container],
                [self.volume(name)],
                {name: ""},
            )
            MEDIA.discover_media(
                registry,
                runner=runner,
                operation=PassthroughScratchOperation(runner, self.root),
                policy=self.policy,
            )

        container["State"]["Status"] = "exited"
        runner = FakeDockerRunner(
            [container],
            [self.volume(name)],
            {name: "V\t/source/18/docker/PG_VERSION\t18\n"},
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "PGDATA conflicts",
        ):
            MEDIA.discover_media(
                registry,
                runner=runner,
                operation=PassthroughScratchOperation(runner, self.root),
                policy=self.policy,
            )

    def test_only_read_only_disjoint_postgres_binds_are_inventory_only(
        self,
    ) -> None:
        name = "postgres-with-init-config"
        container = self.container(
            CONTAINER_A,
            database_mount={
                "Type": "volume",
                "Name": name,
                "Source": name,
                "Destination": "/var/lib/postgresql/data",
                "RW": True,
            },
            pgdata="/var/lib/postgresql/data",
        )
        init_bind = {
            "Type": "bind",
            "Source": "/private/init/10-schema.sql",
            "Destination": "/docker-entrypoint-initdb.d/10-schema.sql",
            "RW": False,
        }
        container["Mounts"].append(init_bind)
        media_id = f"docker-volume:{name}"
        registry = MEDIA.Registry(
            payload=b"fixture",
            digest=MEDIA.sha256_bytes(b"fixture"),
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            service_file_sha256="sha256:" + "7" * 64,
            descriptors=(
                MEDIA.MediaDescriptor(
                    **descriptor(
                        media_id,
                        "nexpoly",
                        disposition="writable-target",
                        user="production_auditor",
                        service="production_audit",
                    )
                ),
            ),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(self.policy),
        )

        runner = FakeDockerRunner(
            [container],
            [self.volume(name)],
            {name: postgres_signature("/source")},
        )
        result = MEDIA.discover_media(
            registry,
            runner=runner,
            operation=PassthroughScratchOperation(runner, self.root),
            policy=self.policy,
        )
        self.assertEqual(sorted(result.media), [media_id])

        for destination, writable in (
            ("/docker-entrypoint-initdb.d/10-schema.sql", True),
            ("/var/lib/postgresql/data/pg_wal", False),
        ):
            container["Mounts"][-1] = {
                **init_bind,
                "Destination": destination,
                "RW": writable,
            }
            runner = FakeDockerRunner(
                [container],
                [self.volume(name)],
                {name: postgres_signature("/source")},
            )
            with self.subTest(destination=destination, writable=writable):
                with self.assertRaisesRegex(
                    MEDIA.MediaEvidenceError,
                    "unclassified persistent bind",
                ):
                    MEDIA.discover_media(
                        registry,
                        runner=runner,
                        operation=PassthroughScratchOperation(
                            runner,
                            self.root,
                        ),
                        policy=self.policy,
                    )

    def test_active_volume_with_unmapped_pgdata_fails_closed(self) -> None:
        name = "opaque-running-database"
        mount = {
            "Type": "volume",
            "Name": name,
            "Source": name,
            "Destination": "/srv/opaque",
            "RW": True,
        }
        container = self.container(
            CONTAINER_A,
            database_mount=mount,
            pgdata="/var/lib/postgresql/data",
        )
        container["Path"] = "/opaque-entrypoint"
        container["Args"] = []
        container["Config"]["Image"] = "custom-runtime:1"
        container["Config"]["Env"] = []
        registry = MEDIA.Registry(
            payload=b"fixture",
            digest=MEDIA.sha256_bytes(b"fixture"),
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            service_file_sha256="sha256:" + "7" * 64,
            descriptors=(),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(self.policy),
        )
        runner = FakeDockerRunner(
            [container],
            [self.volume(name)],
            {name: postgres_signature("/source")},
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "lacks an exact PGDATA container mapping",
        ):
            MEDIA.discover_media(
                registry,
                runner=runner,
                operation=PassthroughScratchOperation(runner, self.root),
                policy=self.policy,
            )

    def test_postgres_command_data_directory_and_extra_wal_fail_closed(
        self,
    ) -> None:
        self.assertEqual(
            MEDIA._container_pgdata(
                {
                    "Path": "docker-entrypoint.sh",
                    "Args": [
                        "postgres",
                        "-c",
                        "data_directory=/srv/database/cluster",
                    ],
                    "Config": {
                        "Image": "postgres:16",
                        "Env": [],
                        "Entrypoint": ["docker-entrypoint.sh"],
                        "Cmd": [
                            "postgres",
                            "-cdata_directory=/srv/database/cluster",
                        ],
                    },
                    "Mounts": [],
                }
            ),
            "/srv/database/cluster",
        )
        primary = "command-primary"
        wal = "command-wal"
        container = self.container(
            CONTAINER_A,
            database_mount={
                "Type": "volume",
                "Name": primary,
                "Source": primary,
                "Destination": "/srv/database",
                "RW": True,
            },
            pgdata="/ignored/by-command",
        )
        container["Path"] = "docker-entrypoint.sh"
        container["Args"] = [
            "postgres",
            "-c",
            "data_directory=/srv/database/cluster",
        ]
        container["Config"]["Env"] = []
        container["Config"]["Cmd"] = list(container["Args"])
        container["Mounts"].append(
            {
                "Type": "volume",
                "Name": wal,
                "Source": wal,
                "Destination": "/srv/database/cluster/pg_wal",
                "RW": True,
            }
        )
        registry = MEDIA.Registry(
            payload=b"fixture",
            digest=MEDIA.sha256_bytes(b"fixture"),
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            service_file_sha256="sha256:" + "7" * 64,
            descriptors=(),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(self.policy),
        )
        runner = FakeDockerRunner(
            [container],
            [self.volume(primary), self.volume(wal)],
            {
                primary: postgres_signature("/source/cluster"),
                wal: "",
            },
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "unclassified persistent volume without PG_VERSION",
        ):
            MEDIA.discover_media(
                registry,
                runner=runner,
                operation=PassthroughScratchOperation(runner, self.root),
                policy=self.policy,
            )

    def test_bind_aggregates_every_reader_not_only_pgdata_container(
        self,
    ) -> None:
        source = self.root / "shared-pgdata"
        private_directory(source)
        private_directory(source / "global")
        private_directory(source / "base")
        private_file(source / "PG_VERSION", b"16\n")
        private_file(source / "global/pg_control", b"fixture-control")
        mount = {
            "Type": "bind",
            "Source": str(source),
            "Destination": "/var/lib/postgresql/data",
            "RW": True,
        }
        postgres = self.container(
            CONTAINER_A,
            database_mount=mount,
            pgdata="/var/lib/postgresql/data",
        )
        reader = self.container(
            CONTAINER_B,
            database_mount={
                **mount,
                "Destination": "/work",
            },
            pgdata="/not/a/mount",
        )
        reader["Path"] = "sleep"
        reader["Args"] = ["infinity"]
        reader["Config"]["Image"] = "busybox:1.36"
        reader["Config"]["Env"] = []
        reader["Config"]["Cmd"] = ["sleep", "infinity"]
        registry = MEDIA.Registry(
            payload=b"fixture",
            digest=MEDIA.sha256_bytes(b"fixture"),
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            service_file_sha256="sha256:" + "7" * 64,
            descriptors=(),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(self.policy),
        )
        runner = FakeDockerRunner([postgres, reader], [], {})
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "unclassified attachment",
        ):
            MEDIA.discover_media(
                registry,
                runner=runner,
                operation=PassthroughScratchOperation(runner, self.root),
                policy=self.policy,
            )

    def test_online_system_identifier_comes_from_exact_attached_container(
        self,
    ) -> None:
        name = "exact-online-volume"
        mount = {
            "Type": "volume",
            "Name": name,
            "Source": name,
            "Destination": "/srv/database",
            "RW": True,
        }
        container = self.container(
            CONTAINER_A,
            database_mount=mount,
            pgdata="/srv/database/pgdata",
        )

        class LiveIdentityRunner(FakeDockerRunner):
            def run(self, arguments, **kwargs):
                values = list(arguments)
                if values[1] == "exec":
                    self.assert_exec = values
                    return self.complete(
                        values,
                        (
                            b"Database system identifier: 7312345678901234567\n"
                        ),
                    )
                return super().run(arguments, **kwargs)

        runner = LiveIdentityRunner(
            [container],
            [self.volume(name)],
            {name: postgres_signature("/source/pgdata")},
        )
        source = MEDIA.DiscoveredMedia(
            media_id=f"docker-volume:{name}",
            kind="docker_volume",
            locator=name,
            data_subpath="pgdata",
            attached=(MEDIA._attached_record(container, mount),),
        )
        self.assertEqual(
            MEDIA._live_source_system_identifier(runner, source),
            "7312345678901234567",
        )
        self.assertIn(CONTAINER_A, runner.assert_exec)
        self.assertIn("/srv/database/pgdata", runner.assert_exec)


class ScratchOperationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="postgres-media-scratch-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.evidence = Path(self.temporary.name) / "evidence"
        private_directory(self.evidence)
        with MEDIA.ScratchLock(self.evidence):
            pass
        self.runner = StatefulScratchDockerRunner()
        self.authority = {
            "registry_sha256": "sha256:" + "1" * 64,
            "service_file_sha256": "sha256:" + "2" * 64,
            "auditor_sha256": "sha256:" + "3" * 64,
            "postgres_image": IMAGE,
            "postgres_image_id": self.runner.image_id,
        }

    def begin(self):
        return MEDIA.ScratchOperation.begin(
            self.evidence,
            runner=self.runner,
            authority=self.authority,
        )

    def test_volume_response_loss_is_adopted_from_intent_and_recovered(self) -> None:
        operation = self.begin()
        self.runner.fail_volume_create_after_create = True
        with self.assertRaises(subprocess.TimeoutExpired):
            operation.create_volume(
                "medium-0000-volume",
                source_media_id="docker-volume:source",
            )
        resource = operation.journal["resources"][0]
        self.assertEqual(resource["state"], "created")
        self.assertIsNotNone(resource["inspect_sha256"])
        self.assertEqual(
            resource["labels"]["io.nexpoly.audit.operation"],
            operation.operation_id,
        )
        self.assertEqual(
            resource["labels"]["io.nexpoly.audit.resource"],
            "medium-0000-volume",
        )
        self.assertEqual(
            resource["labels"]["io.nexpoly.audit.registry"],
            self.authority["registry_sha256"],
        )

        reloaded = MEDIA.ScratchOperation.load(
            self.evidence,
            operation.operation_id,
            runner=self.runner,
        )
        reloaded.recover()
        self.assertEqual(reloaded.journal["phase"], "recovered")
        self.assertEqual(
            reloaded.journal["workspace"]["state"],
            "absent",
        )
        self.assertFalse(self.runner.volumes)

    def test_workspace_publish_crashes_are_recoverable(self) -> None:
        with mock.patch.object(
            MEDIA,
            "_rename_directory_noreplace",
            side_effect=RuntimeError("crash-before-rename"),
        ):
            with self.assertRaisesRegex(RuntimeError, "crash-before-rename"):
                self.begin()
        operation_id = MEDIA._list_scratch_operation_ids(self.evidence)[0]
        interrupted = MEDIA.ScratchOperation.load(
            self.evidence,
            operation_id,
            runner=self.runner,
        )
        interrupted.recover()
        self.assertEqual(interrupted.journal["phase"], "recovered")
        self.assertFalse(interrupted.workspace.exists())
        self.assertFalse(
            any(
                path.name.startswith(f".{interrupted.workspace.name}.stage-")
                for path in interrupted.workspace.parent.iterdir()
            )
        )

        original_update = MEDIA.ScratchOperation._update
        before_ids = set(MEDIA._list_scratch_operation_ids(self.evidence))
        with mock.patch.object(
            MEDIA.ScratchOperation,
            "_update",
            autospec=True,
            side_effect=RuntimeError("crash-after-rename"),
        ):
            with self.assertRaisesRegex(RuntimeError, "crash-after-rename"):
                self.begin()
        new_ids = (
            set(MEDIA._list_scratch_operation_ids(self.evidence))
            - before_ids
        )
        self.assertEqual(len(new_ids), 1)
        operation_id = new_ids.pop()
        published = MEDIA.ScratchOperation.load(
            self.evidence,
            operation_id,
            runner=self.runner,
        )
        self.assertTrue(published.workspace.exists())
        with mock.patch.object(
            MEDIA.ScratchOperation,
            "_update",
            original_update,
        ):
            published.recover()
        self.assertEqual(published.journal["phase"], "recovered")
        self.assertFalse(published.workspace.exists())

    def test_journal_replay_cannot_orphan_operation_labeled_volume(
        self,
    ) -> None:
        operation = self.begin()
        old_head = operation.journal_path.read_bytes()
        volume = operation.create_volume("medium-0000-volume")
        operation.journal_path.write_bytes(old_head)
        os.chmod(operation.journal_path, 0o600)
        reloaded = MEDIA.ScratchOperation.load(
            self.evidence,
            operation.operation_id,
            runner=self.runner,
        )
        self.assertEqual(len(reloaded.journal["resources"]), 1)
        reloaded.recover()
        self.assertNotIn(volume, self.runner.volumes)

        rolled_back = self.begin()
        old_head = rolled_back.journal_path.read_bytes()
        orphan = rolled_back.create_volume("medium-0001-volume")
        for path in (
            self.evidence / MEDIA.SCRATCH_JOURNAL_ROOT_NAME
        ).glob(f"{rolled_back.operation_id}.seq-*.json"):
            sequence = int(path.name.split(".seq-", 1)[1].split("-", 1)[0])
            if sequence > 1:
                path.unlink()
        rolled_back.journal_path.write_bytes(old_head)
        os.chmod(rolled_back.journal_path, 0o600)
        reloaded = MEDIA.ScratchOperation.load(
            self.evidence,
            rolled_back.operation_id,
            runner=self.runner,
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "operation-labeled Docker resources remain",
        ):
            reloaded.recover()
        self.assertIn(orphan, self.runner.volumes)

    def test_volume_swap_before_rm_is_never_deleted(self) -> None:
        operation = self.begin()
        name = operation.create_volume("medium-0000-volume")
        calls = 0

        def swap_during_attachment_scan(
            _operation: MEDIA.ScratchOperation,
            _name: str,
        ) -> list[str]:
            nonlocal calls
            calls += 1
            if calls == 1:
                self.runner.materialize_volume(
                    name,
                    {"foreign": "replacement"},
                )
            return []

        with (
            mock.patch.object(
                MEDIA.ScratchOperation,
                "_volume_attachments",
                autospec=True,
                side_effect=swap_during_attachment_scan,
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "foreign|changed",
            ),
        ):
            operation.remove_resource("medium-0000-volume")
        self.assertIn(name, self.runner.volumes)
        self.assertFalse(
            any(event == ("volume-rm", name) for event in self.runner.events)
        )

    def test_same_process_does_not_retire_an_absent_create_outcome(self) -> None:
        operation = self.begin()
        self.runner.fail_volume_create_before_create = True
        with self.assertRaises(subprocess.TimeoutExpired):
            operation.create_volume("medium-0000-volume")
        self.assertEqual(
            operation._resource("medium-0000-volume")["state"],
            "create-ambiguous",
        )
        self.assertEqual(
            operation.journal["phase"],
            "awaiting-create-resolution",
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "later recovery",
        ):
            operation.abort()
        self.assertTrue(operation.workspace.exists())

        reloaded = MEDIA.ScratchOperation.load(
            self.evidence,
            operation.operation_id,
            runner=self.runner,
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "additional recovery",
        ):
            reloaded.recover()
        self.assertEqual(
            reloaded._resource("medium-0000-volume")["state"],
            "create-absent-confirmation",
        )
        reloaded = MEDIA.ScratchOperation.load(
            self.evidence,
            operation.operation_id,
            runner=self.runner,
        )
        reloaded.recover()
        self.assertEqual(reloaded.journal["phase"], "recovered")
        self.assertEqual(
            reloaded._resource("medium-0000-volume")["state"],
            "absent",
        )

        container_operation = self.begin()
        self.runner.fail_run_before_create = True
        with self.assertRaises(subprocess.TimeoutExpired):
            container_operation.run_container(
                "medium-0001-helper",
                [
                    MEDIA.DOCKER,
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    IMAGE,
                    "true",
                ],
            )
        self.assertEqual(
            container_operation._resource("medium-0001-helper")["state"],
            "create-ambiguous",
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "later recovery",
        ):
            container_operation.remove_resource("medium-0001-helper")
        container_operation = MEDIA.ScratchOperation.load(
            self.evidence,
            container_operation.operation_id,
            runner=self.runner,
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "additional recovery",
        ):
            container_operation.recover()
        container_operation = MEDIA.ScratchOperation.load(
            self.evidence,
            container_operation.operation_id,
            runner=self.runner,
        )
        container_operation.recover()
        self.assertEqual(
            container_operation._resource("medium-0001-helper")["state"],
            "absent",
        )

    def test_workspace_create_response_loss_is_adopted_but_collision_is_not(
        self,
    ) -> None:
        interrupted = self.begin()
        journal = copy.deepcopy(interrupted.journal)
        journal["workspace"]["state"] = "owner-marker-create-intent"
        journal["workspace"]["owner_marker_sha256"] = None
        journal["phase"] = "starting"
        journal["sequence"] += 1
        journal["previous_state_sha256"] = interrupted.journal[
            "journal_sha256"
        ]
        journal = MEDIA._seal_scratch_journal(journal)
        interrupted._persist(journal)
        loaded = MEDIA.ScratchOperation.load(
            self.evidence,
            interrupted.operation_id,
            runner=self.runner,
        )
        loaded.recover()
        self.assertEqual(loaded.journal["workspace"]["state"], "absent")
        self.assertFalse(loaded.workspace.exists())

        collision = self.begin()
        collision_path = collision.workspace
        owner_marker = (
            collision_path / MEDIA.SCRATCH_WORKSPACE_OWNER_NAME
        )
        owner_marker.unlink()
        private_file(owner_marker, b'{"foreign":true}\n')
        blocked = copy.deepcopy(collision.journal)
        blocked["workspace"]["state"] = "create-intent"
        blocked["workspace"]["identity"] = None
        blocked["workspace"]["owner_marker_sha256"] = None
        blocked["phase"] = "starting"
        blocked["blocked_reason"] = None
        blocked["sequence"] += 1
        blocked["previous_state_sha256"] = collision.journal[
            "journal_sha256"
        ]
        blocked = MEDIA._seal_scratch_journal(blocked)
        collision._persist(blocked)
        loaded_collision = MEDIA.ScratchOperation.load(
            self.evidence,
            collision.operation_id,
            runner=self.runner,
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "unowned",
        ):
            loaded_collision.recover()
        self.assertTrue(collision_path.exists())

    def test_recovery_is_container_first_and_rejects_foreign_attachment(self) -> None:
        operation = self.begin()
        volume_name = operation.create_volume("medium-0000-volume")
        container = operation._plan_resource(
            "medium-0000-postgres",
            kind="container",
            spec={
                "postgres_image": IMAGE,
                "postgres_image_id": self.runner.image_id,
                "mounts": [
                    {
                        "kind": "volume",
                        "source": volume_name,
                        "destination": "/var/lib/postgresql/data",
                        "read_only": False,
                    }
                ],
                "network": "none",
                "read_only_rootfs": True,
                "detached": True,
                "command": [],
                "entrypoint": None,
                "environment": [],
                "tmpfs": {},
                "arguments_sha256": "sha256:" + "4" * 64,
            },
            dependencies=("medium-0000-volume",),
        )
        self.runner.materialize_container(
            str(container["name"]),
            container["labels"],
            mounts=[
                {
                    "Type": "volume",
                    "Name": volume_name,
                    "Source": "",
                    "Destination": "/var/lib/postgresql/data",
                    "RW": True,
                }
            ],
        )

        operation.recover()
        kinds = [value[0] for value in self.runner.events]
        self.assertEqual(kinds, ["container-rm", "volume-rm"])
        self.assertFalse(self.runner.containers)
        self.assertFalse(self.runner.volumes)

    def test_foreign_or_unjournaled_resources_are_never_deleted(self) -> None:
        operation = self.begin()
        planned = operation._plan_resource(
            "medium-0000-volume",
            kind="volume",
            spec={"driver": "local", "scope": "local", "options": None},
        )
        self.runner.materialize_volume(
            str(planned["name"]),
            {"io.nexpoly.audit": "true"},
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "foreign",
        ):
            operation.recover()
        self.assertIn(str(planned["name"]), self.runner.volumes)
        self.assertFalse(
            any(event[0] == "volume-rm" for event in self.runner.events)
        )

        second = self.begin()
        owned_name = second.create_volume("medium-0001-volume")
        decoy = "nexpoly-audit-unjournaled-decoy"
        self.runner.materialize_volume(
            decoy,
            {"io.nexpoly.audit": "true"},
        )
        second.recover()
        self.assertNotIn(owned_name, self.runner.volumes)
        self.assertIn(decoy, self.runner.volumes)

    def test_absent_tombstone_cannot_be_rearmed_by_matching_name(self) -> None:
        operation = self.begin()
        name = operation.create_volume("medium-0000-volume")
        resource = copy.deepcopy(operation._resource("medium-0000-volume"))
        operation.remove_resource("medium-0000-volume")
        self.runner.materialize_volume(name, resource["labels"])
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "tombstone",
        ):
            operation.recover()
        self.assertIn(name, self.runner.volumes)

    def test_foreign_attachment_and_replaced_container_id_block_cleanup(self) -> None:
        operation = self.begin()
        volume_name = operation.create_volume("medium-0000-volume")
        self.runner.materialize_container(
            "foreign-reader",
            {},
            mounts=[
                {
                    "Type": "volume",
                    "Name": volume_name,
                    "Source": "",
                    "Destination": "/foreign",
                    "RW": True,
                }
            ],
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "attachments",
        ):
            operation.recover()
        self.assertIn(volume_name, self.runner.volumes)
        self.assertFalse(
            any(event[0] == "volume-rm" for event in self.runner.events)
        )

        second = self.begin()
        completed = second.run_container(
            "medium-0001-postgres",
            [
                MEDIA.DOCKER,
                "run",
                "-d",
                "--network",
                "none",
                "--read-only",
                IMAGE,
                "postgres",
            ],
            detached=True,
        )
        original_id = completed.stdout.decode("ascii").strip()
        recorded = copy.deepcopy(second._resource("medium-0001-postgres"))
        self.runner.containers.pop(original_id)
        replacement = self.runner.materialize_container(
            str(recorded["name"]),
            recorded["labels"],
            mounts=[],
            command=recorded["spec"]["command"],
            environment=recorded["spec"]["environment"],
            tmpfs=recorded["spec"]["tmpfs"],
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "changed",
        ):
            second.recover()
        self.assertIn(str(replacement["Id"]), self.runner.containers)
        self.assertFalse(
            any(
                event == ("container-rm", str(replacement["Id"]))
                for event in self.runner.events
            )
        )

        third = self.begin()
        self.runner.detached_response_id = "f" * 64
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "response identity",
        ):
            third.run_container(
                "medium-0002-postgres",
                [
                    MEDIA.DOCKER,
                    "run",
                    "-d",
                    "--network",
                    "none",
                    "--read-only",
                    IMAGE,
                    "postgres",
                ],
                detached=True,
            )
        owned = third._resource("medium-0002-postgres")
        self.assertEqual(
            third.journal["phase"],
            "blocked-foreign-identity",
        )
        self.assertIn(str(owned["container_id"]), self.runner.containers)
        self.assertFalse(
            any(
                event == ("container-rm", str(owned["container_id"]))
                for event in self.runner.events
            )
        )
        self.runner.detached_response_id = None

    def test_journal_seal_duplicates_symlinks_and_lock_fail_closed(self) -> None:
        operation = self.begin()
        operation._plan_resource(
            "medium-0000-volume",
            kind="volume",
            spec={"driver": "local", "scope": "local", "options": None},
        )
        path = operation.journal_path
        duplicate = copy.deepcopy(operation.journal)
        copied = copy.deepcopy(duplicate["resources"][0])
        copied["resource_key"] = "medium-0001-volume"
        duplicate["resources"].append(copied)
        duplicate = MEDIA._seal_scratch_journal(duplicate)
        path.write_bytes(MEDIA.canonical_json_bytes(duplicate) + b"\n")
        os.chmod(path, 0o600)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "identity",
        ):
            MEDIA.ScratchOperation.load(
                self.evidence,
                operation.operation_id,
                runner=self.runner,
            )

        safe = self.begin()
        real = safe.journal_path.with_suffix(".saved")
        safe.journal_path.rename(real)
        safe.journal_path.symlink_to(real)
        with self.assertRaises(OSError):
            MEDIA.ScratchOperation.load(
                self.evidence,
                safe.operation_id,
                runner=self.runner,
            )

        with MEDIA.ScratchLock(self.evidence):
            with self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "holds",
            ):
                with MEDIA.ScratchLock(self.evidence):
                    pass

    def test_status_and_recover_are_idempotent(self) -> None:
        operation = self.begin()
        operation.create_volume("medium-0000-volume")
        first = MEDIA.recover_scratch_operations(
            self.evidence,
            runner=self.runner,
            operation_id=operation.operation_id,
        )
        second = MEDIA.recover_scratch_operations(
            self.evidence,
            runner=self.runner,
            operation_id=operation.operation_id,
        )
        self.assertEqual(first["recovered_operation_ids"], [operation.operation_id])
        self.assertEqual(first["terminal_operation_ids"], [])
        self.assertEqual(second["recovered_operation_ids"], [])
        self.assertEqual(second["terminal_operation_ids"], [operation.operation_id])
        self.assertEqual(
            MEDIA.scratch_status(self.evidence),
            MEDIA.scratch_status(self.evidence),
        )

    def test_status_preserves_and_recover_discards_incomplete_atomic_update(
        self,
    ) -> None:
        operation = self.begin()
        temporary = (
            operation.journal_path.parent
            / f".{operation.operation_id}.json.tmp-{'a' * 32}"
        )
        private_file(temporary, b'{"partial":')
        status = MEDIA.scratch_status(self.evidence)
        self.assertEqual(status["incomplete_journal_update_count"], 1)
        self.assertTrue(temporary.exists())
        MEDIA.recover_scratch_operations(
            self.evidence,
            runner=self.runner,
            operation_id=operation.operation_id,
        )
        self.assertFalse(temporary.exists())

    def test_recover_and_status_cli_are_repeatable(self) -> None:
        operation = self.begin()
        operation.create_volume("medium-0000-volume")
        with (
            mock.patch.object(
                MEDIA,
                "CommandRunner",
                return_value=self.runner,
            ),
            mock.patch.object(sys, "stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(
                MEDIA.main(
                    [
                        "recover",
                        "--evidence-root",
                        str(self.evidence),
                        "--operation-id",
                        operation.operation_id,
                    ]
                ),
                0,
            )
            recovered = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(
                recovered["recovered_operation_ids"],
                [operation.operation_id],
            )
            output.seek(0)
            output.truncate()
            self.assertEqual(
                MEDIA.main(
                    [
                        "status",
                        "--evidence-root",
                        str(self.evidence),
                        "--operation-id",
                        operation.operation_id,
                    ]
                ),
                0,
            )
            status = json.loads(output.getvalue())
        self.assertEqual(status["operations"][0]["phase"], "recovered")

    def test_build_auto_recovers_before_first_discovery(self) -> None:
        interrupted = self.begin()
        stale_name = interrupted.create_volume("medium-0000-volume")
        registry = MEDIA.Registry(
            payload=b"fixture",
            digest=self.authority["registry_sha256"],
            audit_image=IMAGE,
            auditor_sha256=self.authority["auditor_sha256"],
            service_file_sha256=self.authority["service_file_sha256"],
            descriptors=(),
            required_online_databases=(),
            boundary={},
        )
        discovery = MEDIA.Discovery(
            media={},
            docker_inventory_sha256="sha256:" + "7" * 64,
            backup_inventory_sha256="sha256:" + "8" * 64,
            scanned_volume_names=(),
            scanned_container_ids=(),
        )
        discovery_observed: list[bool] = []

        def discover_after_recovery(*_args, **_kwargs):
            discovery_observed.append(stale_name not in self.runner.volumes)
            return discovery

        with (
            mock.patch.object(
                MEDIA,
                "CommandRunner",
                return_value=self.runner,
            ),
            mock.patch.object(MEDIA, "load_registry", return_value=registry),
            mock.patch.object(
                MEDIA,
                "_private_service_file_digest",
                return_value=registry.service_file_sha256,
            ),
            mock.patch.object(MEDIA, "_validate_audit_image"),
            mock.patch.object(
                MEDIA,
                "discover_media",
                side_effect=discover_after_recovery,
            ),
            mock.patch.object(
                MEDIA,
                "build_evidence",
                return_value={"schema_version": 2, "fixture": True},
            ),
        ):
            result = MEDIA.main(
                [
                    "build",
                    "--registry",
                    str(self.evidence / "registry.json"),
                    "--service-file",
                    str(self.evidence / "pg_service.conf"),
                    "--evidence-root",
                    str(self.evidence),
                    "--expected-registry-sha256",
                    registry.digest,
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(discovery_observed, [True])
        self.assertNotIn(stale_name, self.runner.volumes)


def database_audit(
    database: str,
    user: str,
    *,
    system_identifier: str,
    database_oid: str = "16384",
    database_owner: str | None = None,
    data_directory: str = "/var/lib/postgresql/data",
) -> dict[str, object]:
    ledger = [
        {
            "version": MEDIA.CANONICAL_MIGRATION_LEDGER[0][0],
            "checksum": MEDIA.CANONICAL_MIGRATION_LEDGER[0][1],
        }
    ]
    identity = {
        "database": database,
        "system_identifier": system_identifier,
        "database_oid": database_oid,
        "database_owner": database_owner or user,
        "encoding": "UTF8",
        "collate": "C",
        "ctype": "C",
        "server_version_num": 160004,
        "data_directory": data_directory,
    }
    relation_authority = {
        "relation_kind": "ordinary-table",
        "owner": database_owner or user,
        "columns": [
            {
                "name": name,
                "type": data_type,
                "not_null": not_null,
                "default": default,
            }
            for name, data_type, not_null, default
            in MEDIA.LEDGER_RELATION_COLUMNS
        ],
        "indexes": [
            {"name": name, "definition": definition}
            for name, definition in sorted(
                MEDIA.LEDGER_RELATION_INDEXES.items()
            )
        ],
        "constraints": [
            {
                "name": name,
                "type": value[0],
                "definition": value[1],
            }
            for name, value in sorted(
                MEDIA.LEDGER_RELATION_CONSTRAINTS.items()
            )
        ],
    }
    relation_schema = MEDIA.sha256_bytes(
        MEDIA.canonical_json_bytes(relation_authority)
    )
    empty_rows = MEDIA.sha256_bytes(MEDIA.canonical_json_bytes([]))
    return {
        "database_identity": identity,
        "database_identity_sha256": MEDIA.sha256_bytes(
            MEDIA.canonical_json_bytes(identity)
        ),
        "current_user": user,
        "transaction_read_only": True,
        "role_superuser": False,
        "role_create_db": False,
        "role_create_role": False,
        "ledger": ledger,
        "ledger_sha256": MEDIA.sha256_bytes(
            MEDIA.canonical_json_bytes(ledger)
        ),
        "ledger_relation": {
            "state": "present",
            "row_count": 1,
            "schema_sha256": relation_schema,
            "schema_authority": relation_authority,
            "content_sha256": MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(ledger)
            ),
        },
        "ledger_analysis": {
            "status": "canonical-prefix",
            "canonical_prefix_length": 1,
            "historical_0005_alias_present": False,
            "checksum_mismatches": [],
        },
        "legacy_relation_present": False,
        "legacy_relation": {
            "state": "absent",
            "row_count": None,
            "schema_sha256": None,
            "schema_authority": None,
            "content_sha256": None,
        },
        "migration_0013": {"state": "absent", "checksum": None},
        "requires_0014": False,
        "_unused_empty_digest": empty_rows,
    }


class IsolatedOwnedResourcePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="postgres-media-owned-paths-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.evidence = self.root / "evidence"
        private_directory(self.evidence)
        self.runner = StatefulScratchDockerRunner()
        self.operation = MEDIA.ScratchOperation.begin(
            self.evidence,
            runner=self.runner,
            authority={
                "registry_sha256": "sha256:" + "1" * 64,
                "service_file_sha256": "sha256:" + "2" * 64,
                "auditor_sha256": "sha256:" + "3" * 64,
                "postgres_image": IMAGE,
                "postgres_image_id": self.runner.image_id,
            },
        )
        self.registry = MEDIA.Registry(
            payload=b"fixture",
            digest="sha256:" + "1" * 64,
            audit_image=IMAGE,
            auditor_sha256="sha256:" + "3" * 64,
            service_file_sha256="sha256:" + "2" * 64,
            descriptors=(),
            required_online_databases=(),
            boundary={},
        )

    @staticmethod
    def audit(database: str = "postgres"):
        value = database_audit(
            database,
            "postgres",
            system_identifier="7312345678901234567",
        )
        value.pop("_unused_empty_digest")
        return value

    def resource_keys(self) -> set[str]:
        return {
            str(value["resource_key"])
            for value in self.operation.journal["resources"]
        }

    def scratch_tmpfs(self, resource_key: str) -> dict[str, str]:
        resource = next(
            value
            for value in self.operation.journal["resources"]
            if value["resource_key"] == resource_key
        )
        return resource["spec"]["tmpfs"]

    def test_volume_copy_path_journals_every_helper_and_postgres(self) -> None:
        source = MEDIA.DiscoveredMedia(
            media_id="docker-volume:dormant",
            kind="docker_volume",
            locator="dormant",
            data_subpath=".",
            attached=(),
        )
        current = {
            "name": "dormant",
            "driver": "local",
            "mountpoint": "/var/lib/docker/volumes/dormant/_data",
            "labels_sha256": "sha256:" + "8" * 64,
            "inspect_sha256": "sha256:" + "9" * 64,
            "data_subpath": ".",
            "attached": [],
        }
        descriptor_value = MEDIA.MediaDescriptor(
            media_id=source.media_id,
            kind=source.kind,
            database="postgres",
            database_user="postgres",
            disposition="retained-private-isolated",
            audit_method="isolated-volume-copy-read-only",
            pg_service=None,
        )
        with (
            mock.patch.object(
                MEDIA,
                "_docker_volume_identity",
                return_value=current,
            ),
            mock.patch.object(MEDIA, "_wait_for_postgres"),
            mock.patch.object(
                MEDIA,
                "_audit_container_database",
                return_value=self.audit(),
            ),
        ):
            result = MEDIA._isolated_volume_audit(
                self.runner,
                self.registry,
                descriptor_value,
                source,
                operation=self.operation,
                resource_prefix="medium-0000-volume",
            )
        self.assertEqual(result[1], "sha256:" + "a" * 64)
        self.assertTrue(
            {
                "medium-0000-volume-source-digest-before",
                "medium-0000-volume-volume",
                "medium-0000-volume-copy",
                "medium-0000-volume-clone-digest",
                "medium-0000-volume-chown",
                "medium-0000-volume-hba",
                "medium-0000-volume-postgres",
                "medium-0000-volume-source-digest-after",
            }.issubset(self.resource_keys())
        )
        self.assertEqual(
            self.scratch_tmpfs("medium-0000-volume-postgres"),
            {
                "/var/run/postgresql": (
                    "rw,noexec,nosuid,size=16m,uid=70,gid=70,mode=0700"
                )
            },
        )

    def test_bind_copy_path_journals_copy_hba_and_postgres(self) -> None:
        source_path = self.root / "dormant-bind"
        private_directory(source_path)
        private_file(source_path / "PG_VERSION", b"16\n")
        source = MEDIA.DiscoveredMedia(
            media_id=f"container-bind:{source_path}",
            kind="container_bind",
            locator=str(source_path),
            data_subpath=".",
            attached=(),
        )
        descriptor_value = MEDIA.MediaDescriptor(
            media_id=source.media_id,
            kind=source.kind,
            database="postgres",
            database_user="postgres",
            disposition="retained-private-isolated",
            audit_method="isolated-bind-copy-read-only",
            pg_service=None,
        )
        workspace = self.operation.workspace / "bind-medium"
        workspace.mkdir(mode=0o700)
        with (
            mock.patch.object(MEDIA, "_current_attachments", return_value=[]),
            mock.patch.object(MEDIA, "_wait_for_postgres"),
            mock.patch.object(
                MEDIA,
                "_audit_container_database",
                return_value=self.audit(),
            ),
        ):
            MEDIA._isolated_bind_audit(
                self.runner,
                self.registry,
                descriptor_value,
                source,
                workspace,
                operation=self.operation,
                resource_prefix="medium-0001-bind",
            )
        self.assertTrue(
            {
                "medium-0001-bind-volume",
                "medium-0001-bind-copy",
                "medium-0001-bind-hba",
                "medium-0001-bind-postgres",
            }.issubset(self.resource_keys())
        )
        self.assertEqual(
            self.scratch_tmpfs("medium-0001-bind-postgres"),
            {
                "/var/run/postgresql": (
                    "rw,noexec,nosuid,size=16m,uid=70,gid=70,mode=0700"
                )
            },
        )

    def test_backup_restore_and_image_probe_runs_are_journaled(self) -> None:
        backups = self.root / "backups"
        private_directory(backups)
        backup = backups / "reviewed.dump"
        private_file(backup, b"PGDMP" + b"\0" * 1024)
        source = MEDIA.DiscoveredMedia(
            media_id=f"postgres-backup:{backup}",
            kind="postgres_backup",
            locator=str(backup),
            data_subpath=".",
            attached=(),
            backup_format="postgres-custom-v1",
        )
        descriptor_value = MEDIA.MediaDescriptor(
            media_id=source.media_id,
            kind=source.kind,
            database="postgres",
            database_user="postgres",
            disposition="retained-private-isolated",
            audit_method="isolated-backup-restore-read-only",
            pg_service=None,
        )
        workspace = self.operation.workspace / "backup-medium"
        workspace.mkdir(mode=0o700)
        policy = MEDIA.DiscoveryPolicy(backup_roots=(backups,))
        registry = MEDIA.Registry(
            payload=self.registry.payload,
            digest=self.registry.digest,
            audit_image=self.registry.audit_image,
            auditor_sha256=self.registry.auditor_sha256,
            service_file_sha256=self.registry.service_file_sha256,
            descriptors=(),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(policy),
        )
        with (
            mock.patch.object(MEDIA, "_wait_for_postgres"),
            mock.patch.object(
                MEDIA,
                "_audit_container_database",
                return_value=self.audit(),
            ),
        ):
            MEDIA._isolated_backup_audit(
                self.runner,
                registry,
                descriptor_value,
                source,
                workspace,
                policy=policy,
                operation=self.operation,
                resource_prefix="medium-0002-backup",
            )
        self.assertTrue(
            {
                "medium-0002-backup-volume",
                "medium-0002-backup-postgres",
            }.issubset(self.resource_keys())
        )
        self.assertEqual(
            self.scratch_tmpfs("medium-0002-backup-postgres"),
            {
                "/var/run/postgresql": (
                    "rw,noexec,nosuid,size=16m,uid=70,gid=70,mode=0700"
                )
            },
        )

        self.assertEqual(
            MEDIA._validate_audit_image(
                self.runner,
                IMAGE,
                operation=self.operation,
                resource_prefix="image-check",
            ),
            self.runner.image_id,
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "UID/GID differs",
        ):
            MEDIA._validate_audit_image(
                self.runner,
                IMAGE,
                postgres_uid=999,
                postgres_gid=70,
                operation=self.operation,
                resource_prefix="image-user-drift",
            )
        self.runner.materialize_volume("probe-source", {})
        self.assertEqual(
            MEDIA._probe_volume_pgdata(
                self.runner,
                IMAGE,
                "probe-source",
                operation=self.operation,
                resource_key="discovery-probe",
            ),
            ".",
        )
        self.assertTrue(
            {
                "image-check-version",
                "image-check-postgres-user",
                "image-check-toolchain",
                "discovery-probe",
            }.issubset(self.resource_keys())
        )


def _external_inventory_fixture_v2_retired(
    *,
    dev_ledger: list[dict[str, str]],
    health_ledger: list[dict[str, str]],
    registry_digest: str,
    dev_user: str,
    health_user: str,
) -> dict[str, object]:
    auditor_digest = "sha256:" + "6" * 64
    image_id = "sha256:" + "5" * 64
    service_file_digest = "sha256:" + "4" * 64
    audited_at = "2026-07-17T12:00:00Z"

    def relation(
        ledger: list[dict[str, str]],
        *,
        legacy_present: bool,
    ) -> tuple[dict[str, object], dict[str, object]]:
        ledger_value = {
            "state": "present",
            "row_count": len(ledger),
            "schema_sha256": "sha256:" + "7" * 64,
            "content_sha256": MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(ledger)
            ),
        }
        legacy_value = {
            "state": "present" if legacy_present else "absent",
            "row_count": 9 if legacy_present else None,
            "schema_sha256": (
                "sha256:" + "8" * 64 if legacy_present else None
            ),
            "content_sha256": (
                "sha256:" + "9" * 64 if legacy_present else None
            ),
        }
        return ledger_value, legacy_value

    def database_fields(
        database: str,
        user: str,
        ledger: list[dict[str, str]],
        *,
        legacy_present: bool,
        isolated: bool,
        system_identifier: str,
        system_scope: str,
    ) -> dict[str, object]:
        analysis = MEDIA.analyze_ledger(
            ledger,
            legacy_relation_present=legacy_present,
            isolated=isolated,
        )
        ledger_relation, legacy_relation = relation(
            ledger,
            legacy_present=legacy_present,
        )
        identity = {
            "database": database,
            "system_identifier": system_identifier,
            "system_identifier_scope": system_scope,
            "database_oid": "16384",
            "database_owner": user,
            "encoding": "UTF8",
            "collate": "C",
            "ctype": "C",
            "server_version_num": 160004,
        }
        return {
            "database_identity": identity,
            "database_identity_sha256": MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(identity)
            ),
            "current_user": user,
            "transaction_read_only": True,
            "role_superuser": isolated,
            "role_create_db": isolated,
            "role_create_role": isolated,
            "ledger": ledger,
            "ledger_sha256": MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(ledger)
            ),
            "ledger_relation": ledger_relation,
            "ledger_analysis": {
                key: analysis[key]
                for key in (
                    "status",
                    "canonical_prefix_length",
                    "historical_0005_alias_present",
                    "checksum_mismatches",
                )
            },
            "legacy_relation_present": legacy_present,
            "legacy_relation": legacy_relation,
            "migration_0013": analysis["migration_0013"],
        }

    def volume_record(
        name: str,
        database: str,
        user: str,
        ledger: list[dict[str, str]],
        *,
        disposition: str,
        legacy_present: bool,
        container_id: str,
        system_identifier: str,
    ) -> dict[str, object]:
        media_id = f"docker-volume:{name}"
        source_identity = {
            "name": name,
            "driver": "local",
            "mountpoint": f"/var/lib/docker/volumes/{name}/_data",
            "labels_sha256": "sha256:" + "1" * 64,
            "inspect_sha256": "sha256:" + "2" * 64,
            "data_subpath": ".",
            "attached": [attachment_record(container_id)],
        }
        fields = database_fields(
            database,
            user,
            ledger,
            legacy_present=legacy_present,
            isolated=False,
            system_identifier=system_identifier,
            system_scope="source-cluster",
        )
        source_digest = MEDIA.sha256_bytes(
            MEDIA.canonical_json_bytes(
                {
                    "database_identity": fields["database_identity"],
                    "ledger": fields["ledger"],
                    "ledger_relation": fields["ledger_relation"],
                    "legacy_relation": fields["legacy_relation"],
                }
            )
        )
        return MEDIA._seal_media_record(
            {
                "media_id": media_id,
                "kind": "docker_volume",
                "database": database,
                "disposition": disposition,
            "source_identity_before": source_identity,
            "source_identity_after": source_identity,
            "source_system_identifier": system_identifier,
            "source_content_sha256": source_digest,
                "content_identity_algorithm": "logical-database-identity-v2",
                **fields,
                "audit": {
                    "method": "live-read-only",
                    "complete": True,
                    "auditor_sha256": auditor_digest,
                    "postgres_major": 16,
                    "postgres_uid": 70,
                    "postgres_gid": 70,
                    "postgres_image": IMAGE,
                    "postgres_image_id": image_id,
                    "pg_service_file_sha256": service_file_digest,
                    "audited_at": audited_at,
                    "isolation": {
                        "source_mounted_by_auditor": False,
                        "source_started_by_auditor": False,
                        "transaction_read_only": True,
                    },
                },
            }
        )

    production = volume_record(
        "nexpoly_app_postgres_data",
        "nexpoly",
        "nexpoly_production_auditor",
        health_ledger,
        disposition="writable-target",
        legacy_present=True,
        container_id=CONTAINER_A,
        system_identifier="7312345678901234561",
    )
    development = volume_record(
        "nexpoly_dev_postgres_data",
        "nexpoly_dev",
        dev_user,
        dev_ledger,
        disposition="read-only-online",
        legacy_present=False,
        container_id=CONTAINER_B,
        system_identifier="7312345678901234562",
    )
    health = volume_record(
        "nexpoly_md_health_opt_postgres_data",
        "nexpoly_md_health_opt",
        health_user,
        health_ledger,
        disposition="read-only-online",
        legacy_present=True,
        container_id=CONTAINER_C,
        system_identifier="7312345678901234563",
    )
    backup_path = "/private/backups/nexpoly.dump"
    backup_digest = "sha256:" + "b" * 64
    backup_source = {
        "path": backup_path,
        "device": 1,
        "inode": 2,
        "size_bytes": 1024,
        "mtime_ns": 4,
        "mode": 0o600,
        "uid": os.geteuid(),
        "sha256": backup_digest,
        "format": "postgres-custom-v1",
    }
    backup_fields = database_fields(
        "nexpoly",
        "postgres",
        health_ledger,
        legacy_present=True,
        isolated=True,
        system_identifier="7312345678901234564",
        system_scope="isolated-restore-cluster",
    )
    backup = MEDIA._seal_media_record(
        {
            "media_id": f"postgres-backup:{backup_path}",
            "kind": "postgres_backup",
            "database": "nexpoly",
            "disposition": "retained-private-isolated",
            "source_identity_before": backup_source,
            "source_identity_after": backup_source,
            "source_system_identifier": None,
            "source_content_sha256": backup_digest,
            "content_identity_algorithm": "sha256-file-v1",
            **backup_fields,
            "audit": {
                "method": "isolated-backup-restore-read-only",
                "complete": True,
                "auditor_sha256": auditor_digest,
                "postgres_major": 16,
                "postgres_uid": 70,
                "postgres_gid": 70,
                "postgres_image": IMAGE,
                "postgres_image_id": image_id,
                "pg_service_file_sha256": service_file_digest,
                "audited_at": audited_at,
                "isolation": {
                    "source_opened_with_openat_no_follow": True,
                    "source_passed_to_docker": False,
                    "staged_snapshot_mounted_read_only": True,
                    "source_started_as_postgres": False,
                    "scratch_network": "none",
                    "scratch_destroyed": True,
                    "restore_method": (
                        "pg_restore-no-owner-no-privileges-v1"
                    ),
                },
            },
        }
    )
    media = sorted(
        [production, development, health, backup],
        key=lambda value: value["media_id"],
    )
    by_id = {value["media_id"]: value for value in media}

    def online_database(stack: str, media_id: str) -> dict[str, object]:
        record = by_id[media_id]
        return {
            "stack": stack,
            "media_id": media_id,
            "database": record["database"],
            "current_user": record["current_user"],
            "transaction_read_only": record["transaction_read_only"],
            "role_superuser": record["role_superuser"],
            "role_create_db": record["role_create_db"],
            "role_create_role": record["role_create_role"],
            "system_identifier": record["database_identity"][
                "system_identifier"
            ],
            "database_identity_sha256": record[
                "database_identity_sha256"
            ],
            "ledger": record["ledger"],
            "ledger_sha256": record["ledger_sha256"],
            "legacy_relation_present": record["legacy_relation_present"],
        }

    media_ids = [value["media_id"] for value in media]
    return {
        "schema_version": 2,
        "inventory_complete": True,
        "writable_target": {"stack": "production", "database": "nexpoly"},
        "media_registry": {
            "schema_version": 2,
            "sha256": registry_digest,
            "discovery_boundary_sha256": "sha256:" + "a" * 64,
            "discovery_state_sha256_before": "sha256:" + "e" * 64,
            "discovery_state_sha256_after": "sha256:" + "e" * 64,
            "captured_at": audited_at,
            "expected_media_ids": media_ids,
            "discovered_media_ids": media_ids,
            "docker_inventory_sha256": "sha256:" + "c" * 64,
            "backup_inventory_sha256": "sha256:" + "d" * 64,
            "scanned_volume_names": sorted(
                [
                    "nexpoly_app_postgres_data",
                    "nexpoly_dev_postgres_data",
                    "nexpoly_md_health_opt_postgres_data",
                ]
            ),
            "scanned_container_ids": [
                CONTAINER_A,
                CONTAINER_B,
                CONTAINER_C,
            ],
        },
        "databases": [
            online_database("nexpoly_dev", development["media_id"]),
            online_database(
                "nexpoly_md_health_opt",
                health["media_id"],
            ),
        ],
        "media": media,
        "requires_0014": False,
    }


def external_inventory_fixture(
    *,
    dev_ledger: list[dict[str, str]],
    health_ledger: list[dict[str, str]],
    registry_digest: str,
    dev_user: str,
    health_user: str,
    dev_online: bool = True,
    health_online: bool = True,
) -> dict[str, object]:
    auditor_digest = "sha256:" + "6" * 64
    image_id = "sha256:" + "5" * 64
    service_digest = "sha256:" + "4" * 64
    captured_at = "2026-07-17T12:00:00Z"

    def legacy_authority(owner: str) -> dict[str, object]:
        return {
            "relation_kind": "ordinary-table",
            "owner": owner,
            "columns": [
                {
                    "name": name,
                    "type": data_type,
                    "not_null": not_null,
                    "default": default,
                }
                for name, data_type, not_null, default
                in MEDIA.LEGACY_RELATION_COLUMNS
            ],
            "indexes": [
                {"name": name, "definition": definition}
                for name, definition in sorted(
                    MEDIA.LEGACY_RELATION_INDEXES.items()
                )
            ],
            "constraints": [
                {
                    "name": name,
                    "type": value[0],
                    "definition": value[1],
                }
                for name, value in sorted(
                    MEDIA.LEGACY_RELATION_CONSTRAINTS.items()
                )
            ],
        }

    def audited_database(
        database: str,
        user: str,
        ledger: list[dict[str, str]],
        *,
        system_identifier: str,
        legacy_present: bool,
        isolated: bool,
        scope: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        value = database_audit(
            database,
            user,
            system_identifier=system_identifier,
            database_owner=user,
        )
        value.pop("_unused_empty_digest")
        analysis = MEDIA.analyze_ledger(
            ledger,
            legacy_relation_present=legacy_present,
            isolated=isolated,
        )
        value.update(
            {
                "role_superuser": isolated,
                "role_create_db": isolated,
                "role_create_role": isolated,
                "ledger": ledger,
                "ledger_sha256": MEDIA.sha256_bytes(
                    MEDIA.canonical_json_bytes(ledger)
                ),
                "ledger_relation": {
                    **value["ledger_relation"],
                    "row_count": len(ledger),
                    "content_sha256": MEDIA.sha256_bytes(
                        MEDIA.canonical_json_bytes(ledger)
                    ),
                },
                "ledger_analysis": {
                    key: analysis[key]
                    for key in (
                        "status",
                        "canonical_prefix_length",
                        "historical_0005_alias_present",
                        "checksum_mismatches",
                    )
                },
                "legacy_relation_present": legacy_present,
                "migration_0013": analysis["migration_0013"],
                "requires_0014": analysis["requires_0014"],
            }
        )
        if legacy_present:
            authority = legacy_authority(user)
            value["legacy_relation"] = {
                "state": "present",
                "row_count": 9,
                "schema_sha256": MEDIA.sha256_bytes(
                    MEDIA.canonical_json_bytes(authority)
                ),
                "schema_authority": authority,
                "content_sha256": "sha256:" + "9" * 64,
            }
        authority_record = {
            "name": database,
            "oid": value["database_identity"]["database_oid"],
            "owner": user,
            "allow_connections": True,
            "template": False,
            "audit_role": user,
            "migration_scope": "nexpoly-ledger",
        }
        inventory = [
            {
                key: authority_record[key]
                for key in (
                    "name",
                    "oid",
                    "owner",
                    "allow_connections",
                    "template",
                )
            }
        ]
        bundle = {
            **value,
            "database_inventory": inventory,
            "database_inventory_sha256": MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(inventory)
            ),
            "databases": [
                {
                    **authority_record,
                    "audit_state": "complete",
                    "audit": value,
                }
            ],
        }
        return MEDIA._scope_database_bundle(bundle, scope), authority_record

    def physical_record(
        name: str,
        database: str,
        user: str,
        ledger: list[dict[str, str]],
        *,
        disposition: str,
        legacy_present: bool,
        container_id: str,
        system_identifier: str,
        isolated: bool = False,
    ) -> dict[str, object]:
        bundle, _authority = audited_database(
            database,
            user,
            ledger,
            system_identifier=system_identifier,
            legacy_present=legacy_present,
            isolated=isolated,
            scope=(
                "copied-source-cluster"
                if isolated
                else "source-cluster"
            ),
        )
        source = {
            "name": name,
            "driver": "local",
            "mountpoint": f"/var/lib/docker/volumes/{name}/_data",
            "labels_sha256": "sha256:" + "1" * 64,
            "inspect_sha256": "sha256:" + "2" * 64,
            "data_subpath": ".",
            "attached": (
                []
                if isolated
                else [attachment_record(container_id)]
            ),
        }
        content = MEDIA.sha256_bytes(
            MEDIA.canonical_json_bytes(
                {
                    "database_inventory": bundle["database_inventory"],
                    "databases": bundle["databases"],
                }
            )
        )
        return MEDIA._seal_media_record(
            {
                "record_type": "nexpoly-db",
                "media_id": f"docker-volume:{name}",
                "kind": "docker_volume",
                "classification": "nexpoly-db",
                "database": database,
                "disposition": disposition,
                "source_identity_before": source,
                "source_identity_after": source,
                "source_system_identifier": system_identifier,
                "source_content_sha256": content,
                "content_identity_algorithm": (
                    "postgres-data-directory-tar-sha256-v1"
                    if isolated
                    else "logical-cluster-inventory-v3"
                ),
                "database_inventory": bundle["database_inventory"],
                "database_inventory_sha256": bundle[
                    "database_inventory_sha256"
                ],
                "databases": bundle["databases"],
                **{
                    key: bundle[key]
                    for key in (
                        "database_identity",
                        "database_identity_sha256",
                        "current_user",
                        "transaction_read_only",
                        "role_superuser",
                        "role_create_db",
                        "role_create_role",
                        "ledger",
                        "ledger_sha256",
                        "ledger_relation",
                        "ledger_analysis",
                        "legacy_relation_present",
                        "legacy_relation",
                        "migration_0013",
                    )
                },
                "audit": {
                    "method": (
                        "isolated-volume-copy-read-only"
                        if isolated
                        else "live-read-only"
                    ),
                    "complete": True,
                    "auditor_sha256": auditor_digest,
                    "postgres_major": 16,
                    "postgres_uid": 70,
                    "postgres_gid": 70,
                    "postgres_image": IMAGE,
                    "postgres_image_id": image_id,
                    "pg_service_file_sha256": service_digest,
                    "audited_at": captured_at,
                    "isolation": (
                        {
                            "source_mounted_read_only": True,
                            "source_started_as_postgres": False,
                            "scratch_network": "none",
                            "scratch_destroyed": True,
                            "copy_method": (
                                "readonly-tar-copy-to-disposable-volume-v1"
                            ),
                        }
                        if isolated
                        else {
                            "source_mounted_by_auditor": False,
                            "source_started_by_auditor": False,
                            "transaction_read_only": True,
                        }
                    ),
                },
            }
        )

    production = physical_record(
        "nexpoly_app_postgres_data",
        "nexpoly",
        "nexpoly_production_auditor",
        health_ledger,
        disposition="writable-target",
        legacy_present=True,
        container_id=CONTAINER_A,
        system_identifier="7312345678901234561",
    )
    development = physical_record(
        "nexpoly_dev_postgres_data",
        "nexpoly_dev",
        dev_user if dev_online else "nexpoly_dev",
        dev_ledger,
        disposition=(
            "read-only-online"
            if dev_online
            else "retained-private-isolated"
        ),
        legacy_present=(
            "0007_polytao_jobs"
            in {record["version"] for record in dev_ledger}
            and "0012_drop_polytao_jobs"
            not in {record["version"] for record in dev_ledger}
        ),
        container_id=CONTAINER_B,
        system_identifier="7312345678901234562",
        isolated=not dev_online,
    )
    health = physical_record(
        "nexpoly_md_health_opt_postgres_data",
        "nexpoly_md_health_opt",
        health_user if health_online else "postgres",
        health_ledger,
        disposition=(
            "read-only-online"
            if health_online
            else "retained-private-isolated"
        ),
        legacy_present=True,
        container_id=CONTAINER_C,
        system_identifier="7312345678901234563",
        isolated=not health_online,
    )
    backup_path = "/private/backups/nexpoly.dump"
    backup_bundle, _authority = audited_database(
        "nexpoly",
        "postgres",
        health_ledger,
        system_identifier="7312345678901234564",
        legacy_present=True,
        isolated=True,
        scope="isolated-restore-cluster",
    )
    backup_source = {
        "path": backup_path,
        "device": 1,
        "inode": 2,
        "size_bytes": 1024,
        "mtime_ns": 4,
        "mode": 0o600,
        "uid": os.geteuid(),
        "sha256": "sha256:" + "b" * 64,
        "format": "postgres-custom-v1",
    }
    backup = MEDIA._seal_media_record(
        {
            "record_type": "nexpoly-db",
            "media_id": f"postgres-backup:{backup_path}",
            "kind": "postgres_backup",
            "classification": "nexpoly-db",
            "database": "nexpoly",
            "disposition": "retained-private-isolated",
            "source_identity_before": backup_source,
            "source_identity_after": backup_source,
            "source_system_identifier": None,
            "source_content_sha256": backup_source["sha256"],
            "content_identity_algorithm": "sha256-file-v1",
            "database_inventory": backup_bundle["database_inventory"],
            "database_inventory_sha256": backup_bundle[
                "database_inventory_sha256"
            ],
            "databases": backup_bundle["databases"],
            **{
                key: backup_bundle[key]
                for key in (
                    "database_identity",
                    "database_identity_sha256",
                    "current_user",
                    "transaction_read_only",
                    "role_superuser",
                    "role_create_db",
                    "role_create_role",
                    "ledger",
                    "ledger_sha256",
                    "ledger_relation",
                    "ledger_analysis",
                    "legacy_relation_present",
                    "legacy_relation",
                    "migration_0013",
                )
            },
            "audit": {
                "method": "isolated-backup-restore-read-only",
                "complete": True,
                "auditor_sha256": auditor_digest,
                "postgres_major": 16,
                "postgres_uid": 70,
                "postgres_gid": 70,
                "postgres_image": IMAGE,
                "postgres_image_id": image_id,
                "pg_service_file_sha256": service_digest,
                "audited_at": captured_at,
                "isolation": {
                    "source_opened_with_openat_no_follow": True,
                    "source_passed_to_docker": False,
                    "staged_snapshot_mounted_read_only": True,
                    "source_started_as_postgres": False,
                    "scratch_network": "none",
                    "scratch_destroyed": True,
                    "restore_method": (
                        "pg_restore-no-owner-no-privileges-v1"
                    ),
                },
            },
        }
    )
    media = sorted(
        [production, development, health, backup],
        key=lambda value: value["media_id"],
    )
    by_id = {value["media_id"]: value for value in media}

    def online(stack: str, media_id: str) -> dict[str, object]:
        record = by_id[media_id]
        return {
            "stack": stack,
            "media_id": media_id,
            "database": record["database"],
            "current_user": record["current_user"],
            "transaction_read_only": record["transaction_read_only"],
            "role_superuser": record["role_superuser"],
            "role_create_db": record["role_create_db"],
            "role_create_role": record["role_create_role"],
            "system_identifier": record["database_identity"][
                "system_identifier"
            ],
            "database_identity_sha256": record[
                "database_identity_sha256"
            ],
            "ledger": record["ledger"],
            "ledger_sha256": record["ledger_sha256"],
            "legacy_relation_present": record["legacy_relation_present"],
        }

    media_ids = [value["media_id"] for value in media]
    return {
        "schema_version": 3,
        "inventory_complete": True,
        "writable_target": {"stack": "production", "database": "nexpoly"},
        "media_registry": {
            "schema_version": 3,
            "sha256": registry_digest,
            "discovery_boundary_sha256": "sha256:" + "a" * 64,
            "discovery_state_sha256_before": "sha256:" + "e" * 64,
            "discovery_state_sha256_after": "sha256:" + "e" * 64,
            "captured_at": captured_at,
            "expected_media_ids": media_ids,
            "discovered_media_ids": media_ids,
            "docker_inventory_sha256": "sha256:" + "c" * 64,
            "backup_inventory_sha256": "sha256:" + "d" * 64,
            "scanned_volume_names": sorted(
                [
                    "nexpoly_app_postgres_data",
                    "nexpoly_dev_postgres_data",
                    "nexpoly_md_health_opt_postgres_data",
                ]
            ),
            "scanned_container_ids": [
                CONTAINER_A,
                CONTAINER_B,
                CONTAINER_C,
            ],
        },
        "databases": [
            *(
                [online("nexpoly_dev", development["media_id"])]
                if dev_online
                else []
            ),
            *(
                [online("nexpoly_md_health_opt", health["media_id"])]
                if health_online
                else []
            ),
        ],
        "media": media,
        "requires_0014": any(
            database_record["audit"].get("requires_0014", False)
            for record in media
            for database_record in record["databases"]
        ),
    }


class BuilderAndContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="postgres-media-builder-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.evidence = self.root / "evidence"
        private_directory(self.evidence)
        self.service_file = self.root / "pg_service.conf"
        private_file(self.service_file, b"[fixture]\n")

    @staticmethod
    def volume_source(
        media_id: str,
        container_id: str,
    ) -> MEDIA.DiscoveredMedia:
        name = media_id.removeprefix("docker-volume:")
        return MEDIA.DiscoveredMedia(
            media_id=media_id,
            kind="docker_volume",
            locator=name,
            data_subpath=".",
            attached=(
                attachment_record(container_id),
            ),
        )

    def test_builder_output_is_self_sealed_and_site_validator_accepts_it(self) -> None:
        identifiers = [
            "docker-volume:a-production",
            "docker-volume:b-dev",
            "docker-volume:c-health",
        ]
        descriptors = (
            MEDIA.MediaDescriptor(
                **descriptor(
                    identifiers[0],
                    "nexpoly",
                    disposition="writable-target",
                    user="production_auditor",
                    service="production_audit",
                )
            ),
            MEDIA.MediaDescriptor(
                **descriptor(
                    identifiers[1],
                    "nexpoly_dev",
                    user="dev_auditor",
                    service="dev_audit",
                )
            ),
            MEDIA.MediaDescriptor(
                **descriptor(
                    identifiers[2],
                    "nexpoly_md_health_opt",
                    user="health_auditor",
                    service="health_audit",
                )
            ),
        )
        registry = MEDIA.Registry(
            payload=b"registry-v2",
            digest=MEDIA.sha256_bytes(b"registry-v2"),
            audit_image=IMAGE,
            auditor_sha256="sha256:" + "6" * 64,
            service_file_sha256=MEDIA.sha256_bytes(b"[fixture]\n"),
            descriptors=descriptors,
            required_online_databases=(
                {"stack": "nexpoly_dev", "media_id": identifiers[1]},
                {
                    "stack": "nexpoly_md_health_opt",
                    "media_id": identifiers[2],
                },
            ),
            boundary={"schema_version": 1, "fixture": True},
        )
        sources = {
            identifiers[0]: self.volume_source(identifiers[0], CONTAINER_A),
            identifiers[1]: self.volume_source(identifiers[1], CONTAINER_B),
            identifiers[2]: self.volume_source(identifiers[2], CONTAINER_C),
        }
        discovery = MEDIA.Discovery(
            media=sources,
            docker_inventory_sha256="sha256:" + "1" * 64,
            backup_inventory_sha256="sha256:" + "2" * 64,
            scanned_volume_names=tuple(
                value.locator for value in sources.values()
            ),
            scanned_container_ids=(CONTAINER_A, CONTAINER_B, CONTAINER_C),
        )
        users = {
            "nexpoly": "production_auditor",
            "nexpoly_dev": "dev_auditor",
            "nexpoly_md_health_opt": "health_auditor",
        }

        def source_identity(_runner, source):
            return {
                "name": source.locator,
                "driver": "local",
                "mountpoint": f"/var/lib/docker/volumes/{source.locator}/_data",
                "labels_sha256": "sha256:" + "3" * 64,
                "inspect_sha256": "sha256:" + "4" * 64,
                "data_subpath": ".",
                "attached": [dict(value) for value in source.attached],
            }

        def live_audit(_runner, current, _source):
            authority = dict(current.databases[0])
            value = database_audit(
                current.database,
                users[current.database],
                system_identifier=str(
                    7000000000000000000
                    + list(users).index(current.database)
                ),
                database_oid=str(authority["oid"]),
                database_owner=str(authority["owner"]),
            )
            value.pop("_unused_empty_digest")
            inventory = [
                {
                    key: authority[key]
                    for key in (
                        "name",
                        "oid",
                        "owner",
                        "allow_connections",
                        "template",
                    )
                }
            ]
            return {
                **value,
                "database_inventory": inventory,
                "database_inventory_sha256": MEDIA.sha256_bytes(
                    MEDIA.canonical_json_bytes(inventory)
                ),
                "databases": [
                    {
                        **authority,
                        "audit_state": "complete",
                        "audit": value,
                    }
                ],
            }

        def source_system_identifier(_runner, source):
            return str(
                7000000000000000000
                + identifiers.index(source.media_id)
            )

        with (
            mock.patch.object(
                MEDIA,
                "_validate_audit_image",
                return_value="sha256:" + "5" * 64,
            ),
            mock.patch.object(
                MEDIA,
                "_auditor_digest",
                return_value="sha256:" + "6" * 64,
            ),
            mock.patch.object(
                MEDIA,
                "_live_source_identity",
                side_effect=source_identity,
            ),
            mock.patch.object(
                MEDIA,
                "_live_source_system_identifier",
                side_effect=source_system_identifier,
            ),
            mock.patch.object(
                MEDIA,
                "_run_live_audit",
                side_effect=live_audit,
            ),
            mock.patch.object(
                MEDIA,
                "discover_media",
                return_value=discovery,
            ),
        ):
            envelope = MEDIA.build_evidence(
                registry,
                discovery,
                runner=MEDIA.CommandRunner(),
                service_file=self.service_file,
                evidence_root=self.evidence,
                operation=PassthroughScratchOperation(
                    MEDIA.CommandRunner(),
                    self.evidence,
                ),
                now=lambda: "2026-07-17T12:00:00Z",
            )

        validated = CONTRACTS.validate_external_database_audit(
            envelope,
            expected_users={
                "nexpoly_dev": "dev_auditor",
                "nexpoly_md_health_opt": "health_auditor",
            },
            expected_media_registry_digest=registry.digest,
        )
        self.assertEqual(validated, envelope)
        self.assertEqual(
            len(list(self.evidence.glob("external-database-audit-*.json"))),
            1,
        )
        self.assertEqual(
            envelope["media_registry"]["discovery_state_sha256_before"],
            envelope["media_registry"]["discovery_state_sha256_after"],
        )
        unprojected_live_health = copy.deepcopy(envelope)
        unprojected_live_health["databases"].pop()
        with self.assertRaisesRegex(
            CONTRACTS.SiteHelperContractError,
            "offline nexpoly_md_health_opt medium is not retained-isolated",
        ):
            CONTRACTS.validate_external_database_audit(
                unprojected_live_health,
                expected_users={
                    "nexpoly_dev": "dev_auditor",
                    "nexpoly_md_health_opt": "health_auditor",
                },
                expected_media_registry_digest=registry.digest,
            )
        boundary_drift = copy.deepcopy(envelope)
        boundary_drift["media_registry"][
            "discovery_state_sha256_after"
        ] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(
            CONTRACTS.SiteHelperContractError,
            "boundary changed",
        ):
            CONTRACTS.validate_external_database_audit(
                boundary_drift,
                expected_users={
                    "nexpoly_dev": "dev_auditor",
                    "nexpoly_md_health_opt": "health_auditor",
                },
                expected_media_registry_digest=registry.digest,
            )
        tampered = copy.deepcopy(envelope)
        tampered["media"][0]["database_identity"]["database_oid"] = "999"
        with self.assertRaisesRegex(
            CONTRACTS.SiteHelperContractError,
            "identity digest differs|projection was spliced",
        ):
            CONTRACTS.validate_external_database_audit(
                tampered,
                expected_users={
                    "nexpoly_dev": "dev_auditor",
                    "nexpoly_md_health_opt": "health_auditor",
                },
                expected_media_registry_digest=registry.digest,
            )
        mixed_runtime = copy.deepcopy(envelope)
        mixed_runtime["media"][1]["audit"]["auditor_sha256"] = (
            "sha256:" + "9" * 64
        )
        unsealed = {
            **mixed_runtime["media"][1],
            "audit": {
                key: value
                for key, value in mixed_runtime["media"][1]["audit"].items()
                if key != "evidence_sha256"
            },
        }
        mixed_runtime["media"][1]["audit"]["evidence_sha256"] = (
            MEDIA.sha256_bytes(MEDIA.canonical_json_bytes(unsealed))
        )
        with self.assertRaisesRegex(
            CONTRACTS.SiteHelperContractError,
            "mixes audit runtimes",
        ):
            CONTRACTS.validate_external_database_audit(
                mixed_runtime,
                expected_users={
                    "nexpoly_dev": "dev_auditor",
                    "nexpoly_md_health_opt": "health_auditor",
                },
                expected_media_registry_digest=registry.digest,
            )
        uid_drift = copy.deepcopy(envelope)
        uid_drift["media"][0]["audit"]["postgres_uid"] = 70.0
        with self.assertRaisesRegex(
            CONTRACTS.SiteHelperContractError,
            "audit runtime",
        ):
            CONTRACTS.validate_external_database_audit(
                uid_drift,
                expected_users={
                    "nexpoly_dev": "dev_auditor",
                    "nexpoly_md_health_opt": "health_auditor",
                },
                expected_media_registry_digest=registry.digest,
            )

    def test_site_validator_accepts_retained_health_with_only_dev_online(
        self,
    ) -> None:
        registry_digest = "sha256:" + "c" * 64
        envelope = external_inventory_fixture(
            dev_ledger=[
                {
                    "version": version,
                    "checksum": checksum,
                }
                for version, checksum in MEDIA.CANONICAL_MIGRATION_LEDGER[:12]
            ],
            health_ledger=[
                {
                    "version": version,
                    "checksum": checksum,
                }
                for version, checksum in MEDIA.CANONICAL_MIGRATION_LEDGER[:8]
            ],
            registry_digest=registry_digest,
            dev_user="dev_auditor",
            health_user="unused_health_auditor",
            health_online=False,
        )
        validated = CONTRACTS.validate_external_database_audit(
            envelope,
            expected_users={
                "nexpoly_dev": "dev_auditor",
                "nexpoly_md_health_opt": "unused_health_auditor",
            },
            expected_media_registry_digest=registry_digest,
        )
        self.assertEqual(validated, envelope)
        self.assertEqual(
            [record["stack"] for record in envelope["databases"]],
            ["nexpoly_dev"],
        )
        health = next(
            record
            for record in envelope["media"]
            if record.get("database") == "nexpoly_md_health_opt"
        )
        self.assertEqual(
            health["audit"]["method"],
            "isolated-volume-copy-read-only",
        )

    def test_site_validator_accepts_empty_online_projection_when_both_are_isolated(
        self,
    ) -> None:
        registry_digest = "sha256:" + "c" * 64
        envelope = external_inventory_fixture(
            dev_ledger=[
                {
                    "version": version,
                    "checksum": checksum,
                }
                for version, checksum in MEDIA.CANONICAL_MIGRATION_LEDGER[:9]
            ],
            health_ledger=[
                {
                    "version": version,
                    "checksum": checksum,
                }
                for version, checksum in MEDIA.CANONICAL_MIGRATION_LEDGER[:8]
            ],
            registry_digest=registry_digest,
            dev_user="unused_dev_auditor",
            health_user="unused_health_auditor",
            dev_online=False,
            health_online=False,
        )
        validated = CONTRACTS.validate_external_database_audit(
            envelope,
            expected_users={
                "nexpoly_dev": "unused_dev_auditor",
                "nexpoly_md_health_opt": "unused_health_auditor",
            },
            expected_media_registry_digest=registry_digest,
        )
        self.assertEqual(validated, envelope)
        self.assertEqual(envelope["databases"], [])
        for stack in ("nexpoly_dev", "nexpoly_md_health_opt"):
            record = next(
                value
                for value in envelope["media"]
                if value.get("database") == stack
            )
            self.assertEqual(
                record["disposition"],
                "retained-private-isolated",
            )
            self.assertEqual(
                record["audit"]["method"],
                "isolated-volume-copy-read-only",
            )


class BackupAuditRunner(MEDIA.CommandRunner):
    def __init__(self, database_payload: bytes) -> None:
        self.database_payload = database_payload
        self.calls: list[list[str]] = []

    def run(
        self,
        arguments,
        *,
        input_bytes=None,
        timeout=120,
        check=True,
        env=None,
    ):
        del input_bytes, timeout, check, env
        values = list(arguments)
        self.calls.append(values)
        if values[1:3] == ["volume", "create"]:
            return FakeDockerRunner.complete(values, (values[-1] + "\n").encode())
        if values[1:3] == ["run", "-d"]:
            return FakeDockerRunner.complete(values, (CONTAINER_A + "\n").encode())
        if values[1:3] == ["container", "rm"]:
            return FakeDockerRunner.complete(values)
        if values[1:3] == ["container", "inspect"]:
            return subprocess.CompletedProcess(
                values,
                1,
                stdout=b"",
                stderr=b"Error: No such container",
            )
        if values[1:3] == ["volume", "rm"]:
            return FakeDockerRunner.complete(values)
        if values[1:3] == ["volume", "inspect"]:
            return subprocess.CompletedProcess(
                values,
                1,
                stdout=b"",
                stderr=b"Error: no such volume",
            )
        if values[1] == "exec" and "pg_isready" in values:
            return FakeDockerRunner.complete(values)
        if values[1] == "exec" and "psql" in values:
            return FakeDockerRunner.complete(values, self.database_payload)
        if values[1] == "exec":
            return FakeDockerRunner.complete(values)
        raise AssertionError(f"unexpected backup-audit command: {values!r}")


class IsolatedBackupOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="postgres-backup-audit-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.backups = self.root / "backups"
        self.workspace = self.root / "workspace"
        private_directory(self.backups)
        private_directory(self.workspace)

    @staticmethod
    def database_payload() -> bytes:
        records = [
            {
                "record_type": "database",
                "database": "nexpoly",
                "current_user": "postgres",
                "transaction_read_only": True,
                "role_superuser": True,
                "role_create_db": True,
                "role_create_role": True,
                "system_identifier": "7312345678901234567",
                "database_oid": "16384",
                "database_owner": "postgres",
                "encoding": "UTF8",
                "collate": "C",
                "ctype": "C",
                "server_version_num": 160004,
                "data_directory": "/var/lib/postgresql/data",
            },
            {
                "record_type": "ledger",
                "rows": [],
                "relation": None,
            },
            {
                "record_type": "legacy_relation",
                "present": False,
                "relation": None,
                "rows": [],
            },
        ]
        return b"\n".join(
            json.dumps(value, sort_keys=True).encode("utf-8")
            for value in records
        ) + b"\n"

    def test_private_dump_uses_fixed_network_none_restore_and_cleans_scratch(self) -> None:
        backup = self.backups / "reviewed.dump"
        private_file(backup, b"PGDMP" + b"\0" * 1024)
        source = MEDIA.DiscoveredMedia(
            media_id=f"postgres-backup:{backup}",
            kind="postgres_backup",
            locator=str(backup),
            data_subpath=".",
            attached=(),
            backup_format="postgres-custom-v1",
        )
        descriptor_value = MEDIA.MediaDescriptor(
            media_id=source.media_id,
            kind="postgres_backup",
            database="nexpoly",
            database_user="postgres",
            disposition="retained-private-isolated",
            audit_method="isolated-backup-restore-read-only",
            pg_service=None,
        )
        registry = MEDIA.Registry(
            payload=b"fixture",
            digest=MEDIA.sha256_bytes(b"fixture"),
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            service_file_sha256="sha256:" + "7" * 64,
            descriptors=(descriptor_value,),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(
                MEDIA.DiscoveryPolicy(backup_roots=(self.backups,))
            ),
        )
        policy = MEDIA.DiscoveryPolicy(backup_roots=(self.backups,))
        runner = BackupAuditRunner(self.database_payload())
        operation = PassthroughScratchOperation(runner, self.workspace)

        database, source_digest, identity, after, isolation = (
            MEDIA._isolated_backup_audit(
                runner,
                registry,
                descriptor_value,
                source,
                self.workspace,
                policy=policy,
                operation=operation,
                resource_prefix="medium-0000-backup",
            )
        )

        self.assertEqual(database["ledger_analysis"]["status"], "empty-isolated")
        self.assertEqual(source_digest, identity["sha256"])
        self.assertEqual(identity, after)
        self.assertTrue(isolation["scratch_destroyed"])
        run_command = next(
            values for values in runner.calls if values[1:3] == ["run", "-d"]
        )
        self.assertIn("none", run_command)
        self.assertIn(
            f"type=bind,src={self.workspace},dst=/source-audit,readonly",
            run_command,
        )
        restore = next(
            values for values in runner.calls if "pg_restore" in values
        )
        for fixed in (
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--strict-names",
        ):
            self.assertIn(fixed, restore)
        self.assertTrue(
            any(values[1:3] == ["container", "rm"] for values in runner.calls)
        )
        self.assertTrue(
            any(values[1:3] == ["volume", "rm"] for values in runner.calls)
        )

@unittest.skipUnless(
    os.environ.get("NEXPOLY_RUN_POSTGRES_MEDIA_INTEGRATION") == "1",
    "enable the real PostgreSQL media integration explicitly",
)
@unittest.skipUnless(
    os.environ.get("NEXPOLY_POSTGRES_MEDIA_TEST_ACK")
    == "ephemeral-localhost-only",
    "acknowledge that only ephemeral localhost Docker resources may be used",
)
class RealDockerPostgresIntegrationTests(unittest.TestCase):
    def pinned_image(self) -> str:
        image = os.environ["NEXPOLY_TEST_POSTGRES_IMAGE"]
        if MEDIA.IMAGE_RE.fullmatch(image) is None:
            self.fail("NEXPOLY_TEST_POSTGRES_IMAGE must be a full image digest")
        runner = MEDIA.CommandRunner()
        MEDIA._local_audit_image_id(runner, image)
        identity = runner.run(
            [
                MEDIA.DOCKER,
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "/bin/sh",
                image,
                "-ceu",
                (
                    "printf '%s:%s\\n' "
                    '"$(id -u postgres)" "$(id -g postgres)"'
                ),
            ]
        ).stdout.decode("ascii", "strict").strip()
        self.assertEqual(identity, "70:70")
        return image

    def test_dormant_volume_is_copied_audited_and_source_cas_verified(self) -> None:
        image = self.pinned_image()
        runner = MEDIA.CommandRunner()
        volume = MEDIA._temp_name("integration-source")
        initializer_name = MEDIA._temp_name("integration-init")
        initializer: str | None = None
        runner.run([MEDIA.DOCKER, "volume", "create", "--", volume])
        try:
            completed = runner.run(
                [
                    MEDIA.DOCKER,
                    "run",
                    "-d",
                    "--name",
                    initializer_name,
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    (
                        "/var/run/postgresql:rw,noexec,nosuid,size=16m,"
                        "uid=70,gid=70,mode=0700"
                    ),
                    "--mount",
                    f"type=volume,src={volume},dst=/var/lib/postgresql/data",
                    "--env",
                    "POSTGRES_HOST_AUTH_METHOD=trust",
                    "--env",
                    "POSTGRES_USER=postgres",
                    image,
                    "postgres",
                    "-c",
                    "listen_addresses=",
                    "-c",
                    "unix_socket_directories=/var/run/postgresql",
                ],
                timeout=120,
            )
            initializer = completed.stdout.decode("ascii", "strict").strip()
            MEDIA._wait_for_postgres(
                runner,
                initializer,
                database="postgres",
                user="postgres",
            )
            runner.run(
                [
                    MEDIA.DOCKER,
                    "container",
                    "rm",
                    "-f",
                    "--",
                    initializer,
                ]
            )
            initializer = None
            source = MEDIA.DiscoveredMedia(
                media_id=f"docker-volume:{volume}",
                kind="docker_volume",
                locator=volume,
                data_subpath=".",
                attached=(),
            )
            descriptor_value = MEDIA.MediaDescriptor(
                media_id=source.media_id,
                kind="docker_volume",
                database="postgres",
                database_user="postgres",
                disposition="retained-private-isolated",
                audit_method="isolated-volume-copy-read-only",
                pg_service=None,
                databases=(
                    {
                        "name": "postgres",
                        "oid": "5",
                        "owner": "postgres",
                        "allow_connections": True,
                        "template": False,
                        "audit_role": "postgres",
                        "migration_scope": "nexpoly-ledger",
                    },
                ),
            )
            registry = MEDIA.Registry(
                payload=b"integration",
                digest=MEDIA.sha256_bytes(b"integration"),
                audit_image=image,
                auditor_sha256=MEDIA._auditor_digest(),
                service_file_sha256="sha256:" + "7" * 64,
                descriptors=(descriptor_value,),
                required_online_databases=(),
                boundary={},
            )
            with tempfile.TemporaryDirectory(
                prefix="postgres-media-real-volume-"
            ) as temporary:
                evidence = Path(temporary) / "evidence"
                private_directory(evidence)
                operation = MEDIA.ScratchOperation.begin(
                    evidence,
                    runner=runner,
                    authority={
                        "registry_sha256": registry.digest,
                        "service_file_sha256": registry.service_file_sha256,
                        "auditor_sha256": registry.auditor_sha256,
                        "postgres_image": image,
                        "postgres_image_id": MEDIA._local_audit_image_id(
                            runner,
                            image,
                        ),
                    },
                )
                database, source_digest, _identity, _after, isolation = (
                    MEDIA._isolated_volume_audit(
                        runner,
                        registry,
                        descriptor_value,
                        source,
                        operation=operation,
                        resource_prefix="medium-0000-volume",
                    )
                )
                operation.abort()
            self.assertEqual(database["database_identity"]["database"], "postgres")
            self.assertRegex(source_digest, r"^sha256:[0-9a-f]{64}$")
            self.assertTrue(isolation["scratch_destroyed"])
        finally:
            runner.run(
                [
                    MEDIA.DOCKER,
                    "container",
                    "rm",
                    "-f",
                    "--",
                    initializer or initializer_name,
                ],
                check=False,
            )
            runner.run(
                [MEDIA.DOCKER, "volume", "rm", "--", volume],
                check=False,
            )

    def test_custom_dump_is_restored_and_audited_in_disposable_pg16(self) -> None:
        image = self.pinned_image()
        runner = MEDIA.CommandRunner()
        source_volume = MEDIA._temp_name("integration-dump-source")
        source_container_name = MEDIA._temp_name("integration-dump-postgres")
        source_container: str | None = None
        runner.run(
            [MEDIA.DOCKER, "volume", "create", "--", source_volume]
        )
        try:
            completed = runner.run(
                [
                    MEDIA.DOCKER,
                    "run",
                    "-d",
                    "--name",
                    source_container_name,
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    (
                        "/var/run/postgresql:rw,noexec,nosuid,size=16m,"
                        "uid=70,gid=70,mode=0700"
                    ),
                    "--mount",
                    (
                        f"type=volume,src={source_volume},"
                        "dst=/var/lib/postgresql/data"
                    ),
                    "--env",
                    "POSTGRES_HOST_AUTH_METHOD=trust",
                    "--env",
                    "POSTGRES_USER=postgres",
                    image,
                    "postgres",
                    "-c",
                    "listen_addresses=",
                    "-c",
                    "unix_socket_directories=/var/run/postgresql",
                ],
                timeout=120,
            )
            source_container = completed.stdout.decode().strip()
            MEDIA._wait_for_postgres(
                runner,
                source_container,
                database="postgres",
                user="postgres",
            )
            runner.run(
                [
                    MEDIA.DOCKER,
                    "exec",
                    "--user",
                    "postgres",
                    source_container,
                    "createdb",
                    "-h",
                    "/var/run/postgresql",
                    "-U",
                    "postgres",
                    "nexpoly",
                ]
            )
            migration = MEDIA.CANONICAL_MIGRATION_LEDGER[0]
            runner.run(
                [
                    MEDIA.DOCKER,
                    "exec",
                    "--user",
                    "postgres",
                    "-i",
                    source_container,
                    "psql",
                    "-X",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-h",
                    "/var/run/postgresql",
                    "-U",
                    "postgres",
                    "-d",
                    "nexpoly",
                ],
                input_bytes=(
                    "CREATE SCHEMA governance;"
                    "CREATE TABLE governance.schema_migrations("
                    "version text PRIMARY KEY, checksum text NOT NULL,"
                    "applied_at timestamptz NOT NULL DEFAULT now());"
                    "INSERT INTO governance.schema_migrations VALUES "
                    f"('{migration[0]}','{migration[1]}');"
                ).encode(),
            )
            dump = runner.run(
                [
                    MEDIA.DOCKER,
                    "exec",
                    "--user",
                    "postgres",
                    source_container,
                    "pg_dump",
                    "-Fc",
                    "-h",
                    "/var/run/postgresql",
                    "-U",
                    "postgres",
                    "-d",
                    "nexpoly",
                ],
                timeout=600,
            ).stdout
        finally:
            runner.run(
                [
                    MEDIA.DOCKER,
                    "container",
                    "rm",
                    "-f",
                    "-v",
                    "--",
                    source_container or source_container_name,
                ],
                check=False,
            )
            runner.run(
                [MEDIA.DOCKER, "volume", "rm", "--", source_volume],
                check=False,
            )

        with tempfile.TemporaryDirectory(
            prefix="postgres-media-real-dump-"
        ) as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            backups = root / "backups"
            private_directory(backups)
            evidence = root / "evidence"
            private_directory(evidence)
            path = backups / "nexpoly.dump"
            private_file(path, dump)
            source = MEDIA.DiscoveredMedia(
                media_id=f"postgres-backup:{path}",
                kind="postgres_backup",
                locator=str(path),
                data_subpath=".",
                attached=(),
                backup_format="postgres-custom-v1",
            )
            descriptor_value = MEDIA.MediaDescriptor(
                media_id=source.media_id,
                kind="postgres_backup",
                database="nexpoly",
                database_user="postgres",
                disposition="retained-private-isolated",
                audit_method="isolated-backup-restore-read-only",
                pg_service=None,
                databases=(
                    {
                        "name": "nexpoly",
                        "oid": "16384",
                        "owner": "postgres",
                        "allow_connections": True,
                        "template": False,
                        "audit_role": "postgres",
                        "migration_scope": "nexpoly-ledger",
                    },
                    {
                        "name": "postgres",
                        "oid": "5",
                        "owner": "postgres",
                        "allow_connections": True,
                        "template": False,
                        "audit_role": "postgres",
                        "migration_scope": "adjacent-record-only",
                    },
                ),
            )
            registry = MEDIA.Registry(
                payload=b"integration-dump",
                digest=MEDIA.sha256_bytes(b"integration-dump"),
                audit_image=image,
                auditor_sha256=MEDIA._auditor_digest(),
                service_file_sha256="sha256:" + "7" * 64,
                descriptors=(descriptor_value,),
                required_online_databases=(),
                boundary=MEDIA.seal_discovery_boundary(
                    MEDIA.DiscoveryPolicy(backup_roots=(backups,))
                ),
            )
            operation = MEDIA.ScratchOperation.begin(
                evidence,
                runner=runner,
                authority={
                    "registry_sha256": registry.digest,
                    "service_file_sha256": registry.service_file_sha256,
                    "auditor_sha256": registry.auditor_sha256,
                    "postgres_image": image,
                    "postgres_image_id": MEDIA._local_audit_image_id(
                        runner,
                        image,
                    ),
                },
            )
            workspace = operation.workspace / "medium-0000"
            workspace.mkdir(mode=0o700)
            database, source_digest, identity, after, isolation = (
                MEDIA._isolated_backup_audit(
                    runner,
                    registry,
                    descriptor_value,
                    source,
                    workspace,
                    policy=MEDIA.DiscoveryPolicy(
                        backup_roots=(backups,)
                    ),
                    operation=operation,
                    resource_prefix="medium-0000-backup",
                )
            )
            operation.abort()
            self.assertEqual(
                database["ledger_analysis"]["canonical_prefix_length"],
                1,
            )
            self.assertEqual(source_digest, identity["sha256"])
            self.assertEqual(identity, after)
            self.assertTrue(isolation["scratch_destroyed"])


if __name__ == "__main__":
    unittest.main()

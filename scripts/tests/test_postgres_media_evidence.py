from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
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
) -> dict[str, object]:
    if media_id.startswith("docker-volume:"):
        kind = "docker_volume"
    elif media_id.startswith("container-bind:"):
        kind = "container_bind"
    else:
        kind = "postgres_backup"
    return {
        "media_id": media_id,
        "kind": kind,
        "database": database,
        "database_user": user,
        "disposition": disposition,
        "audit_method": method,
        "pg_service": service,
    }


def registry_document(
    policy,
    descriptors: list[dict[str, object]],
    *,
    dev_media: str,
    health_media: str,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "discovery_boundary": policy.document(),
        "audit_runtime": {
            "auditor_sha256": MEDIA._auditor_digest(),
            "pg_service_file_sha256": "sha256:" + "7" * 64,
            "postgres_image": IMAGE,
            "postgres_major": 16,
        },
        "expected_media": descriptors,
        "required_online_databases": [
            {"stack": "nexpoly_dev", "media_id": dev_media},
            {"stack": "nexpoly_md_health_opt", "media_id": health_media},
        ],
    }


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


def private_file(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    os.chmod(path, 0o600)


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
                    user="health_auditor",
                    service="health_audit",
                ),
            ],
            dev_media=identifiers[1],
            health_media=identifiers[2],
        )

    def test_registry_v2_accepts_only_the_complete_compiled_boundary(self) -> None:
        value = self.valid_document()
        self.write_registry(value)
        loaded = MEDIA.load_registry(
            self.registry,
            policy=self.policy,
            private_root=self.config,
        )
        self.assertEqual(loaded.boundary, self.policy.document())
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
        )
        second, second_scan = MEDIA._walk_backup_root(
            self.backup_b,
            self.policy,
        )
        self.assertEqual(first[0].backup_format, "postgres-custom-v1")
        self.assertEqual(second[0].backup_format, "postgres-tar-v1")
        self.assertEqual(len(first_scan) + len(second_scan), 3)

        (self.backup_a / "hostile.dump").symlink_to(custom)
        with self.assertRaises(OSError):
            MEDIA._walk_backup_root(self.backup_a, self.policy)


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
                production: "/source/pgdata/PG_VERSION\n",
                development: "/source/PG_VERSION\n",
                dormant: "/source/pgdata/PG_VERSION\n",
                unrelated: "",
            },
        )
        identifiers = sorted(
            [
                f"container-bind:{bind}",
                f"docker-volume:{development}",
                f"docker-volume:{dormant}",
                f"docker-volume:{production}",
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
            boundary=self.policy.document(),
        )

        result = MEDIA.discover_media(
            registry,
            runner=runner,
            policy=self.policy,
        )

        self.assertEqual(sorted(result.media), identifiers)
        self.assertEqual(
            result.media[f"docker-volume:{dormant}"].data_subpath,
            "pgdata",
        )
        self.assertIn(unrelated, result.scanned_volume_names)
        self.assertNotIn(f"docker-volume:{unrelated}", result.media)
        self.assertEqual(set(runner.probed), set(runner.volumes))

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
            boundary=self.policy.document(),
        )
        runner = FakeDockerRunner(
            [container],
            [self.volume(name)],
            {name: "/source/PG_VERSION\n"},
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "lacks an exact PGDATA container mapping",
        ):
            MEDIA.discover_media(
                registry,
                runner=runner,
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
            {name: "/source/pgdata/PG_VERSION\n"},
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


def database_audit(
    database: str,
    user: str,
    *,
    system_identifier: str,
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
        "database_oid": "16384",
        "database_owner": user,
        "encoding": "UTF8",
        "collate": "C",
        "ctype": "C",
        "server_version_num": 160004,
    }
    relation_schema = MEDIA.sha256_bytes(b"ledger-schema")
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
            "content_sha256": None,
        },
        "migration_0013": {"state": "absent", "checksum": None},
        "requires_0014": False,
        "_unused_empty_digest": empty_rows,
    }


def external_inventory_fixture(
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

        def live_audit(_runner, current, _service):
            value = database_audit(
                current.database,
                users[current.database],
                system_identifier=str(
                    7000000000000000000
                    + list(users).index(current.database)
                ),
            )
            value.pop("_unused_empty_digest")
            return value

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
            "identity digest differs",
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
            boundary={},
        )
        policy = MEDIA.DiscoveryPolicy(backup_roots=(self.backups,))
        runner = BackupAuditRunner(self.database_payload())

        database, source_digest, identity, after, isolation = (
            MEDIA._isolated_backup_audit(
                runner,
                registry,
                descriptor_value,
                source,
                self.workspace,
                policy=policy,
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

    def test_cleanup_attempts_volume_even_when_container_removal_fails(self) -> None:
        class CleanupRunner(BackupAuditRunner):
            def run(self, arguments, **kwargs):
                result = super().run(arguments, **kwargs)
                values = list(arguments)
                if values[1:3] == ["container", "inspect"]:
                    return FakeDockerRunner.complete(values, b"[{}]")
                return result

        runner = CleanupRunner(self.database_payload())
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "not completely removed",
        ):
            MEDIA._cleanup_scratch(
                runner,
                container=CONTAINER_A,
                volume="fixture-volume",
            )
        self.assertTrue(
            any(values[1:3] == ["volume", "rm"] for values in runner.calls)
        )


@unittest.skipUnless(
    os.environ.get("NEXPOLY_RUN_POSTGRES_MEDIA_INTEGRATION") == "1",
    "set NEXPOLY_RUN_POSTGRES_MEDIA_INTEGRATION=1 with a preloaded pinned image",
)
class RealDockerPostgresIntegrationTests(unittest.TestCase):
    def pinned_image(self) -> str:
        image = os.environ["NEXPOLY_TEST_POSTGRES_IMAGE"]
        if MEDIA.IMAGE_RE.fullmatch(image) is None:
            self.fail("NEXPOLY_TEST_POSTGRES_IMAGE must be a full image digest")
        MEDIA._validate_audit_image(MEDIA.CommandRunner(), image)
        return image

    def test_dormant_volume_is_copied_audited_and_source_cas_verified(self) -> None:
        image = self.pinned_image()
        runner = MEDIA.CommandRunner()
        volume = MEDIA._temp_name("integration-source")
        runner.run([MEDIA.DOCKER, "volume", "create", "--", volume])
        try:
            runner.run(
                [
                    MEDIA.DOCKER,
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--mount",
                    f"type=volume,src={volume},dst=/var/lib/postgresql/data",
                    "--user",
                    "postgres",
                    "--entrypoint",
                    "/usr/lib/postgresql/16/bin/initdb",
                    image,
                    "-D",
                    "/var/lib/postgresql/data",
                    "--auth-local=trust",
                    "--auth-host=reject",
                ],
                timeout=600,
            )
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
            database, source_digest, _identity, _after, isolation = (
                MEDIA._isolated_volume_audit(
                    runner,
                    registry,
                    descriptor_value,
                    source,
                )
            )
            self.assertEqual(database["database_identity"]["database"], "postgres")
            self.assertRegex(source_digest, r"^sha256:[0-9a-f]{64}$")
            self.assertTrue(isolation["scratch_destroyed"])
        finally:
            MEDIA._remove_volume(runner, volume)

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
                        "uid=999,gid=999,mode=0700"
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
                    "version text PRIMARY KEY, checksum text NOT NULL);"
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
            MEDIA._cleanup_scratch(
                runner,
                container=source_container or source_container_name,
                volume=source_volume,
            )

        with tempfile.TemporaryDirectory(
            prefix="postgres-media-real-dump-"
        ) as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            backups = root / "backups"
            workspace = root / "workspace"
            private_directory(backups)
            private_directory(workspace)
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
            )
            registry = MEDIA.Registry(
                payload=b"integration-dump",
                digest=MEDIA.sha256_bytes(b"integration-dump"),
                audit_image=image,
                auditor_sha256=MEDIA._auditor_digest(),
                service_file_sha256="sha256:" + "7" * 64,
                descriptors=(descriptor_value,),
                required_online_databases=(),
                boundary={},
            )
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
                )
            )
            self.assertEqual(
                database["ledger_analysis"]["canonical_prefix_length"],
                1,
            )
            self.assertEqual(source_digest, identity["sha256"])
            self.assertEqual(identity, after)
            self.assertTrue(isolation["scratch_destroyed"])


if __name__ == "__main__":
    unittest.main()

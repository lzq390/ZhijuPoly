from __future__ import annotations

import contextlib
import copy
from collections.abc import Callable
from dataclasses import replace
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import types
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

IMAGE = MEDIA.POSTGRES_AUDIT_IMAGES[16]
IMAGE_ID = "sha256:" + IMAGE.rsplit("sha256:", 1)[1]
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
    online_admin_role: str | None = None,
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
    if method == "live-read-only" and online_admin_role is None:
        online_admin_role = "postgres"
    return {
        "media_id": media_id,
        "kind": kind,
        "database": database,
        "database_user": user,
        "disposition": disposition,
        "audit_method": method,
        "online_admin_role": online_admin_role,
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
    writable = [
        record
        for record in descriptors
        if record["disposition"] == "writable-target"
    ]
    assert len(writable) == 1
    return {
        "schema_version": 5,
        "media_authority_rules_sha256": "sha256:" + "8" * 64,
        "reviewed_content_inventory_sha256": "sha256:" + "9" * 64,
        "production_identity": {
            "stack": "production",
            "database": "nexpoly",
            "kind": "docker_volume",
            "media_id": writable[0]["media_id"],
            "postgres_major": 16,
            "system_identifier": "7659245354718314530",
        },
        "discovery_boundary": MEDIA.seal_discovery_boundary(policy),
        "audit_runtime": {
            "auditor_sha256": MEDIA._auditor_digest(),
            "postgres_image": MEDIA.POSTGRES_AUDIT_IMAGES[16],
            "postgres_images": {
                str(major): {
                    "digest_ref": image,
                    "image_id": "sha256:"
                    + ("5" if major == 16 else str(major % 10)) * 64,
                }
                for major, image in MEDIA.POSTGRES_AUDIT_IMAGES.items()
            },
            "postgres_image_id": "sha256:" + "5" * 64,
            "postgres_major": 16,
            "postgres_uid": 70,
            "postgres_gid": 70,
        },
        "expected_media": sorted(
            descriptors,
            key=lambda record: str(record["media_id"]),
        ),
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


def database_credentials_document(
    *,
    container_id: str = CONTAINER_A,
    system_identifier: str = "7612345678901234567",
    online_admin_role: str = "postgres",
    postgres_major: int = 16,
    password: str = "sealed-fixture-password",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "records": [
            {
                "container_id": container_id,
                "cluster_system_identifier": system_identifier,
                "online_admin_role": online_admin_role,
                "postgres_major": postgres_major,
                "password": password,
            }
        ],
    }


@contextlib.contextmanager
def inherited_database_credentials(
    document: dict[str, object],
):
    with tempfile.TemporaryDirectory(
        prefix="postgres-media-credentials-"
    ) as raw:
        path = Path(raw) / "postgres-media-credentials.json"
        payload = MEDIA.canonical_json_bytes(document) + b"\n"
        private_file(path, payload)
        descriptor = os.open(path, os.O_RDONLY)
        try:
            with mock.patch.dict(
                os.environ,
                {
                    MEDIA.DATABASE_CREDENTIALS_FD_ENV: str(descriptor),
                    MEDIA.DATABASE_CREDENTIALS_DIGEST_ENV: (
                        MEDIA.sha256_bytes(payload)
                    ),
                },
                clear=False,
            ):
                yield path, descriptor, payload
        finally:
            os.close(descriptor)


def postgres_signature(root: str, major: int = 16) -> str:
    return (
        f"B\t{root}/base\n"
        f"C\t{root}/global/pg_control\n"
        f"V\t{root}/PG_VERSION\t{major}\n"
    )


def role_security_fields(
    database: str,
    *,
    superuser: bool,
    ledger_present: bool = True,
    legacy_present: bool = False,
) -> dict[str, object]:
    direct_acl: list[dict[str, object]] = []
    settings: list[str] = []
    if not superuser:
        direct_acl = [
            {
                "object_kind": "database",
                "object_name": database,
                "privilege": "CONNECT",
                "grantable": False,
            },
            {
                "object_kind": "function",
                "object_name": "pg_catalog.pg_control_system()",
                "privilege": "EXECUTE",
                "grantable": False,
            },
        ]
        if ledger_present:
            direct_acl.extend(
                [
                    {
                        "object_kind": "relation",
                        "object_name": "governance.schema_migrations",
                        "privilege": "SELECT",
                        "grantable": False,
                    },
                    {
                        "object_kind": "schema",
                        "object_name": "governance",
                        "privilege": "USAGE",
                        "grantable": False,
                    },
                ]
            )
        if legacy_present:
            direct_acl.extend(
                [
                    {
                        "object_kind": "relation",
                        "object_name": "generation.polytao_jobs",
                        "privilege": "SELECT",
                        "grantable": False,
                    },
                    {
                        "object_kind": "schema",
                        "object_name": "generation",
                        "privilege": "USAGE",
                        "grantable": False,
                    },
                ]
            )
        settings = [
            "default_transaction_read_only=on",
            "lock_timeout=5s",
            "statement_timeout=5min",
        ]
    return {
        "event_triggers_disabled": None,
        "event_triggers": [],
        "role_memberships": [],
        "role_incoming_memberships": [],
        "role_settings": settings,
        "role_owned_objects": [],
        "role_direct_acl": sorted(
            direct_acl,
            key=MEDIA.canonical_json_bytes,
        ),
        "role_default_acl": [],
        "role_effective_persistent_write": [],
    }


def database_startup_fields(
    data_directory: str,
    *,
    isolated: bool = False,
) -> dict[str, object]:
    return {
        "jit": False,
        "shared_preload_libraries": "",
        "session_preload_libraries": "",
        "local_preload_libraries": "",
        "dynamic_library_path": "$libdir",
        "archive_mode": "off",
        "archive_command": "(disabled)" if isolated else "",
        "archive_cleanup_command": "",
        "restore_command": "/bin/false" if isolated else "",
        "recovery_end_command": "",
        "ssl_passphrase_command": "",
        "ssl_passphrase_command_supports_reload": "off",
        "jit_provider": "llvmjit",
        "config_file": f"{data_directory}/postgresql.conf",
        "hba_file": f"{data_directory}/pg_hba.conf",
        "ident_file": f"{data_directory}/pg_ident.conf",
        "data_directory": data_directory,
        "config_source_files": [
            f"{data_directory}/postgresql.conf",
        ],
        "config_errors": [],
    }


def audited_startup_fields(
    data_directory: str,
    *,
    online: bool = False,
) -> dict[str, object]:
    raw = database_startup_fields(
        data_directory,
        isolated=not online,
    )
    return {
        "jit": raw["jit"],
        "shared_preload_libraries": raw["shared_preload_libraries"],
        "session_preload_libraries": raw[
            "session_preload_libraries"
        ],
        "local_preload_libraries": raw["local_preload_libraries"],
        "dynamic_library_path": raw["dynamic_library_path"],
        "archive_mode": raw["archive_mode"],
        "archive_command": raw["archive_command"],
        "archive_cleanup_command": raw["archive_cleanup_command"],
        "restore_command": raw["restore_command"],
        "recovery_end_command": raw["recovery_end_command"],
        "ssl_passphrase_command": raw["ssl_passphrase_command"],
        "ssl_passphrase_command_supports_reload": raw[
            "ssl_passphrase_command_supports_reload"
        ],
        "jit_provider": raw["jit_provider"],
        "config_file": raw["config_file"],
        "hba_file": raw["hba_file"],
        "ident_file": raw["ident_file"],
        "config_source_files": raw["config_source_files"],
        "config_errors": [],
        "independent_configuration_tree_sha256": (
            "sha256:" + "7" * 64 if online else None
        ),
        "verification": (
            "pinned-read-only-config-parse-v1"
            if online
            else "owned-isolated-cluster-v1"
        ),
    }


def trusted_startup_projection(
    data_directory: str = "/var/lib/postgresql/data",
) -> dict[str, object]:
    raw = database_startup_fields(data_directory)
    return {
        "settings": {
            key: raw[key]
            for key in MEDIA.TRUSTED_SERVER_STARTUP_SETTINGS
        },
        "configuration_tree_sha256": "sha256:" + "7" * 64,
        "configuration_files": [
            {
                "path": f"{data_directory}/postgresql.conf",
                "sha256": "sha256:" + "6" * 64,
            }
        ],
        "volume_name": "fixture",
        "mount_destination": "/var/lib/postgresql/data",
        "pgdata": data_directory,
    }


TRUSTED_SERVER_IMAGE_ID = (
    "sha256:"
    "57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
)
TRUSTED_SERVER_CLASSIC_IMAGE_ID = (
    MEDIA.POSTGRES_AUDIT_LINUX_AMD64_CHAINS[16]["config"]
)
TRUSTED_SERVER_PLATFORM_MANIFEST = (
    MEDIA.POSTGRES_AUDIT_LINUX_AMD64_CHAINS[16]["manifest"]
)
TRUSTED_SERVER_REPO_DIGEST = MEDIA.TRUSTED_POSTGRES_SERVER_IMAGES[16][
    TRUSTED_SERVER_IMAGE_ID
]


def trusted_server_epoch_fixture(
    *,
    pgdata: str = "/var/lib/postgresql/data",
    mount_destination: str = "/var/lib/postgresql/data",
    local_image_id: str = TRUSTED_SERVER_IMAGE_ID,
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    base_environment = [
        (
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:"
            "/usr/bin:/sbin:/bin"
        ),
        "GOSU_VERSION=1.17",
        "LANG=en_US.utf8",
        "PG_MAJOR=16",
        "PG_VERSION=16.10-1.pgdg13+1",
        "PGDATA=/var/lib/postgresql/data",
    ]
    runtime_environment = [
        *[
            value
            for value in base_environment
            if not value.startswith("PGDATA=")
        ],
        f"PGDATA={pgdata}",
        "POSTGRES_USER=postgres",
        "POSTGRES_DB=postgres",
        "POSTGRES_PASSWORD=fixture-secret",
    ]
    sandbox_id = "a" * 64
    container = {
        "Id": CONTAINER_A,
        "Name": "/trusted-postgres",
        "Image": local_image_id,
        "Path": "docker-entrypoint.sh",
        "Args": ["postgres"],
        "RestartCount": 0,
        "Config": {
            "Image": TRUSTED_SERVER_REPO_DIGEST,
            "Entrypoint": ["docker-entrypoint.sh"],
            "Cmd": ["postgres"],
            "Env": runtime_environment,
            "User": "",
            "WorkingDir": "",
        },
        "HostConfig": {
            "Privileged": False,
            "CapAdd": None,
            "PidMode": "",
            "UsernsMode": "",
            "UTSMode": "",
            "IpcMode": "private",
            "NetworkMode": "bridge",
            "Devices": [],
            "DeviceRequests": None,
            "SecurityOpt": ["no-new-privileges"],
            "Runtime": "runc",
            "CgroupnsMode": "private",
            "Tmpfs": {
                "/var/run/postgresql": (
                    "rw,noexec,nosuid,size=16777216,mode=0700"
                )
            },
        },
        "State": {
            "Status": "running",
            "Pid": 4242,
            "StartedAt": "2026-07-17T10:01:00.000000000Z",
        },
        "Mounts": [
            {
                "Type": "volume",
                "Name": "fixture",
                "Source": "/var/lib/docker/volumes/fixture/_data",
                "Destination": mount_destination,
                "RW": True,
                "Propagation": "",
            }
        ],
        "NetworkSettings": {
            "SandboxID": sandbox_id,
            "SandboxKey": (
                "/var/run/docker/netns/" + sandbox_id[:12]
            ),
        },
    }
    image = {
        "Id": local_image_id,
        "RepoDigests": [TRUSTED_SERVER_REPO_DIGEST],
        "Config": {
            "Entrypoint": ["docker-entrypoint.sh"],
            "Cmd": ["postgres"],
            "Env": base_environment,
            "User": "",
            "WorkingDir": "",
        },
    }
    volume = {
        "Name": "fixture",
        "Driver": "local",
        "Scope": "local",
        "Options": None,
        "Labels": {},
        "Mountpoint": "/var/lib/docker/volumes/fixture/_data",
    }
    return container, image, volume


class TrustedServerEpochRunner(MEDIA.CommandRunner):
    def __init__(
        self,
        *,
        container: dict[str, object],
        image: dict[str, object],
        volume: dict[str, object],
        startup_overrides: dict[str, str] | None = None,
    ) -> None:
        self.containers = {str(container["Id"]): container}
        self.image = image
        self.volume = volume
        self.diff = b""
        self.calls: list[list[str]] = []
        self.startup_overrides = startup_overrides or {}

    @staticmethod
    def complete(
        arguments: list[str],
        stdout: bytes = b"",
        *,
        returncode: int = 0,
        stderr: bytes = b"",
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            arguments,
            returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def run(self, arguments, **_kwargs):
        values = list(arguments)
        self.calls.append(values)
        if values[1:4] == ["ps", "-aq", "--no-trunc"]:
            return self.complete(
                values,
                (
                    "\n".join(sorted(self.containers))
                    + "\n"
                ).encode("ascii"),
            )
        if values[1:3] == ["container", "inspect"]:
            identity = values[-1]
            value = self.containers.get(identity)
            if value is None:
                return self.complete(
                    values,
                    returncode=1,
                    stderr=b"Error: no such container",
                )
            return self.complete(
                values,
                json.dumps([value]).encode("utf-8"),
            )
        if values[1:3] == ["image", "inspect"]:
            return self.complete(
                values,
                json.dumps([self.image]).encode("utf-8"),
            )
        if values[1:3] == ["volume", "inspect"]:
            return self.complete(
                values,
                json.dumps([self.volume]).encode("utf-8"),
            )
        if values[1:2] == ["diff"]:
            return self.complete(values, self.diff)
        if values[1:2] == ["run"]:
            container = next(iter(self.containers.values()))
            pgdata = next(
                value.split("=", 1)[1]
                for value in container["Config"]["Env"]
                if value.startswith("PGDATA=")
            )
            settings = {
                "shared_preload_libraries": "",
                "session_preload_libraries": "",
                "local_preload_libraries": "",
                "dynamic_library_path": "$libdir",
                "archive_mode": "off",
                "archive_command": "",
                "archive_cleanup_command": "",
                "restore_command": "",
                "recovery_end_command": "",
                "ssl_passphrase_command": "",
                "ssl_passphrase_command_supports_reload": "off",
                "jit_provider": "llvmjit",
                "config_file": f"{pgdata}/postgresql.conf",
                "hba_file": f"{pgdata}/pg_hba.conf",
                "ident_file": f"{pgdata}/pg_ident.conf",
                "data_directory": pgdata,
                **self.startup_overrides,
            }
            output = "".join(
                f"S\t{key}\t{settings[key]}\n"
                for key in MEDIA.TRUSTED_SERVER_STARTUP_SETTINGS
            )
            output += (
                "C\tpostgresql.conf\t"
                + "6" * 64
                + "\n"
            )
            return self.complete(values, output.encode("utf-8"))
        raise AssertionError(f"unexpected trusted-server command: {values!r}")


class TrustedOnlineServerEpochTests(unittest.TestCase):
    PROCESS_EPOCH = {
        "pid": 4242,
        "start_time_ticks": "123456",
        "mountinfo_sha256": "sha256:" + "8" * 64,
    }

    def runner(self) -> TrustedServerEpochRunner:
        container, image, volume = trusted_server_epoch_fixture()
        return TrustedServerEpochRunner(
            container=container,
            image=image,
            volume=volume,
        )

    def test_exact_static_server_process_namespace_and_volume_are_sealed(
        self,
    ) -> None:
        for local_image_id in (
            TRUSTED_SERVER_IMAGE_ID,
            TRUSTED_SERVER_CLASSIC_IMAGE_ID,
        ):
            with self.subTest(local_image_id=local_image_id):
                container, image, volume = trusted_server_epoch_fixture(
                    local_image_id=local_image_id,
                )
                runner = TrustedServerEpochRunner(
                    container=container,
                    image=image,
                    volume=volume,
                )
                with mock.patch.object(
                    MEDIA,
                    "_process_namespace_epoch",
                    return_value=self.PROCESS_EPOCH,
                ):
                    epoch = MEDIA._trusted_server_runtime_epoch(
                        runner,
                        CONTAINER_A,
                        postgres_major=16,
                    )
                self.assertEqual(epoch["image_id"], local_image_id)
                self.assertEqual(
                    epoch["server_repo_digest"],
                    TRUSTED_SERVER_REPO_DIGEST,
                )
                self.assertEqual(epoch["process"], self.PROCESS_EPOCH)
                self.assertEqual(epoch["critical_layer_diff"], [])
                self.assertEqual(epoch["volumes"][0]["name"], "fixture")

    def test_official_platform_chains_allow_only_index_or_config_id(
        self,
    ) -> None:
        all_chain_digests: list[str] = []
        for major, chain in (
            MEDIA.POSTGRES_AUDIT_LINUX_AMD64_CHAINS.items()
        ):
            with self.subTest(postgres_major=major):
                self.assertEqual(
                    set(chain),
                    {"index", "manifest", "config"},
                )
                self.assertEqual(len(set(chain.values())), 3)
                self.assertTrue(
                    all(
                        MEDIA.DIGEST_RE.fullmatch(value)
                        for value in chain.values()
                    )
                )
                expected = "postgres@" + chain["index"]
                trusted = MEDIA.TRUSTED_POSTGRES_SERVER_IMAGES[major]
                self.assertEqual(trusted[chain["index"]], expected)
                self.assertEqual(trusted[chain["config"]], expected)
                self.assertNotIn(chain["manifest"], trusted)
                self.assertTrue(
                    MEDIA.POSTGRES_AUDIT_IMAGES[major].endswith(
                        "@" + chain["index"]
                    )
                )
                all_chain_digests.extend(chain.values())
        self.assertEqual(
            len(set(all_chain_digests)),
            len(all_chain_digests),
        )

        container, image, volume = trusted_server_epoch_fixture(
            local_image_id=TRUSTED_SERVER_CLASSIC_IMAGE_ID,
        )
        runner = TrustedServerEpochRunner(
            container=container,
            image=image,
            volume=volume,
        )
        with (
            mock.patch.object(
                MEDIA,
                "_process_namespace_epoch",
                return_value=self.PROCESS_EPOCH,
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "runtime is incomplete",
            ),
        ):
            MEDIA._trusted_server_runtime_epoch(
                runner,
                CONTAINER_A,
                postgres_major=15,
            )

    def test_hostile_launch_namespace_mount_peer_and_layer_drift_fail_closed(
        self,
    ) -> None:
        cases = {
            "loader-environment": lambda runner: runner.containers[
                CONTAINER_A
            ]["Config"]["Env"].append(
                "LD_PRELOAD=/tmp/hostile.so"
            ),
            "launch-arguments": lambda runner: runner.containers[
                CONTAINER_A
            ].update({"Args": ["postgres", "-c", "jit=on"]}),
            "privileged-runtime": lambda runner: runner.containers[
                CONTAINER_A
            ]["HostConfig"].update({"Privileged": True}),
            "binary-tmpfs": lambda runner: runner.containers[
                CONTAINER_A
            ]["HostConfig"]["Tmpfs"].update(
                {"/usr/local/bin": "rw"}
            ),
            "binary-mount": lambda runner: runner.containers[
                CONTAINER_A
            ]["Mounts"].append(
                {
                    "Type": "bind",
                    "Source": "/tmp/hostile",
                    "Destination": "/usr/bin",
                    "RW": False,
                    "Propagation": "rprivate",
                }
            ),
            "loader-control-diff": lambda runner: setattr(
                runner,
                "diff",
                b"C /etc/ld.so.preload\n",
            ),
            "protected-layer-diff": lambda runner: setattr(
                runner,
                "diff",
                b"C /usr/local/bin/postgres\n",
            ),
            "shared-network-peer": self._add_shared_network_peer,
            "repository-digest-drift": lambda runner: runner.image.update(
                {
                    "RepoDigests": [
                        "postgres@sha256:" + "0" * 64
                    ]
                }
            ),
            "platform-manifest-is-not-runtime-id": (
                lambda runner: (
                    runner.containers[CONTAINER_A].update(
                        {"Image": TRUSTED_SERVER_PLATFORM_MANIFEST}
                    ),
                    runner.image.update(
                        {"Id": TRUSTED_SERVER_PLATFORM_MANIFEST}
                    ),
                )
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                runner = self.runner()
                mutate(runner)
                with (
                    mock.patch.object(
                        MEDIA,
                        "_process_namespace_epoch",
                        return_value=self.PROCESS_EPOCH,
                    ),
                    self.assertRaises(MEDIA.MediaEvidenceError),
                ):
                    MEDIA._trusted_server_runtime_epoch(
                        runner,
                        CONTAINER_A,
                        postgres_major=16,
                    )

    @staticmethod
    def _add_shared_network_peer(
        runner: TrustedServerEpochRunner,
    ) -> None:
        target = runner.containers[CONTAINER_A]
        runner.containers[CONTAINER_B] = {
            "Id": CONTAINER_B,
            "State": {"Status": "running"},
            "NetworkSettings": {
                "SandboxID": target["NetworkSettings"]["SandboxID"],
            },
        }

    def test_startup_parser_accepts_exact_volume_and_pg18_parent_volume(
        self,
    ) -> None:
        cases = (
            (16, "/var/lib/postgresql/data", "/var/lib/postgresql/data"),
            (18, "/var/lib/postgresql/18/docker", "/var/lib/postgresql"),
        )
        for major, pgdata, destination in cases:
            with self.subTest(postgres_major=major):
                container, image, volume = trusted_server_epoch_fixture(
                    pgdata=pgdata,
                    mount_destination=destination,
                )
                runner = TrustedServerEpochRunner(
                    container=container,
                    image=image,
                    volume=volume,
                )
                projection = MEDIA._trusted_server_startup_projection(
                    runner,
                    container_id=CONTAINER_A,
                    postgres_major=major,
                )
                self.assertEqual(projection["pgdata"], pgdata)
                self.assertEqual(
                    projection["mount_destination"],
                    destination,
                )
                command = runner.calls[-1]
                self.assertIn("--read-only", command)
                self.assertIn("--cap-drop", command)
                self.assertIn(
                    f"{MEDIA.POSTGRES_UID}:{MEDIA.POSTGRES_GID}",
                    command,
                )
                self.assertIn(
                    (
                        "type=volume,src=fixture,"
                        f"dst={destination},readonly"
                    ),
                    command,
                )
                self.assertIn(
                    MEDIA.POSTGRES_AUDIT_IMAGES[major],
                    command,
                )

    def test_startup_parser_rejects_preload_and_client_cas_rejects_drift(
        self,
    ) -> None:
        hostile_settings = {
            "shared_preload_libraries": "hostile",
            "archive_mode": "on",
            "archive_command": "/tmp/archive %p",
            "archive_cleanup_command": "/tmp/cleanup %r",
            "restore_command": "/tmp/restore %f",
            "recovery_end_command": "/tmp/recovery-end",
            "ssl_passphrase_command": "/tmp/passphrase",
            "ssl_passphrase_command_supports_reload": "on",
            "jit_provider": "hostilejit",
        }
        for setting, value in hostile_settings.items():
            with self.subTest(setting=setting):
                container, image, volume = (
                    trusted_server_epoch_fixture()
                )
                runner = TrustedServerEpochRunner(
                    container=container,
                    image=image,
                    volume=volume,
                    startup_overrides={setting: value},
                )
                with self.assertRaisesRegex(
                    MEDIA.MediaEvidenceError,
                    "untrusted code",
                ):
                    MEDIA._trusted_server_startup_projection(
                        runner,
                        container_id=CONTAINER_A,
                        postgres_major=16,
                    )

        before = trusted_startup_projection()
        after = {
            **before,
            "configuration_tree_sha256": "sha256:" + "9" * 64,
        }
        execution_runner = mock.Mock(spec=MEDIA.CommandRunner)
        execution_runner.run.return_value = subprocess.CompletedProcess(
            ["docker"],
            0,
            stdout=b"fixture\n",
            stderr=b"",
        )
        sink: dict[str, object] = {}
        with (
            mock.patch.object(
                MEDIA,
                "_trusted_server_runtime_epoch",
                side_effect=[
                    {"epoch": "stable"},
                    {"epoch": "stable"},
                ],
            ),
            mock.patch.object(
                MEDIA,
                "_local_audit_image_id",
                side_effect=[IMAGE_ID, IMAGE_ID],
            ),
            mock.patch.object(
                MEDIA,
                "_trusted_server_startup_projection",
                side_effect=[before, after],
            ),
            mock.patch.object(
                MEDIA,
                "_online_container_connection",
                return_value=("postgres", "postgres", True),
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "execution epoch changed",
            ),
        ):
            MEDIA._run_trusted_psql(
                execution_runner,
                container_id=CONTAINER_A,
                postgres_major=16,
                pgoptions=MEDIA.PSQL_AUDIT_PGOPTIONS,
                arguments=["-U", "postgres", "-d", "postgres"],
                expected_image_id=IMAGE_ID,
                startup_sink=sink,
            )
        self.assertEqual(sink, {})


class InheritedDatabaseCredentialTests(unittest.TestCase):
    class RecordingRunner(MEDIA.CommandRunner):
        def __init__(
            self,
            *,
            system_identifier: str = "7612345678901234567",
            accepted_pgpass: bytes | None = None,
        ) -> None:
            self.system_identifier = system_identifier
            self.accepted_pgpass = accepted_pgpass
            self.commands: list[list[str]] = []
            self.environments: list[dict[str, str]] = []
            self.inputs: list[bytes] = []

        def run(
            self,
            arguments,
            *,
            input_bytes=None,
            env=None,
            **_kwargs,
        ):
            command = list(arguments)
            payload = input_bytes or b""
            environment = dict(env or {})
            self.commands.append(command)
            self.environments.append(environment)
            self.inputs.append(payload)
            pgpass = payload.split(b"\n", 1)[0]
            if (
                self.accepted_pgpass is not None
                and pgpass != self.accepted_pgpass
            ):
                raise MEDIA.MediaEvidenceError(
                    "command failed (/usr/bin/docker): "
                    "psql: password authentication failed"
                )
            if any(
                "pg_control_system" in value
                for value in command
            ):
                stdout = (self.system_identifier + "\n").encode("ascii")
            else:
                stdout = b"fixture-result\n"
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=stdout,
                stderr=b"",
            )

    @staticmethod
    @contextlib.contextmanager
    def stable_client_patches():
        startup = trusted_startup_projection()
        with (
            mock.patch.object(
                MEDIA,
                "_trusted_server_runtime_epoch",
                side_effect=[
                    {"epoch": "stable"},
                    {"epoch": "stable"},
                ],
            ),
            mock.patch.object(
                MEDIA,
                "_local_audit_image_id",
                side_effect=[IMAGE_ID, IMAGE_ID],
            ),
            mock.patch.object(
                MEDIA,
                "_trusted_server_startup_projection",
                side_effect=[startup, startup],
            ),
            mock.patch.object(
                MEDIA,
                "_online_container_connection",
                return_value=("postgres", "postgres", False),
            ),
        ):
            yield

    def test_envelope_binds_container_system_admin_and_major(self) -> None:
        document = database_credentials_document()
        with inherited_database_credentials(document):
            credentials = MEDIA._inherited_database_credentials()
            self.assertIsNotNone(credentials)
            assert credentials is not None
            self.assertEqual(len(credentials), 1)
            self.assertEqual(credentials[0].container_id, CONTAINER_A)
            self.assertEqual(
                credentials[0].cluster_system_identifier,
                "7612345678901234567",
            )
            self.assertEqual(credentials[0].online_admin_role, "postgres")
            self.assertEqual(credentials[0].postgres_major, 16)
            for values, message in (
                (
                    {
                        "container_id": CONTAINER_B,
                        "postgres_major": 16,
                        "online_admin_role": "postgres",
                    },
                    "exact container",
                ),
                (
                    {
                        "container_id": CONTAINER_A,
                        "postgres_major": 15,
                        "online_admin_role": "postgres",
                    },
                    "target identity",
                ),
                (
                    {
                        "container_id": CONTAINER_A,
                        "postgres_major": 16,
                        "online_admin_role": "other_admin",
                    },
                    "target identity",
                ),
            ):
                with self.assertRaisesRegex(
                    MEDIA.MediaEvidenceError,
                    message,
                ):
                    MEDIA._inherited_database_credential(**values)

    def test_digest_mutation_and_fd_path_swap_fail_closed(self) -> None:
        document = database_credentials_document()
        with inherited_database_credentials(document) as (
            path,
            _descriptor,
            _payload,
        ):
            os.chmod(path, 0o600)
            path.write_bytes(
                MEDIA.canonical_json_bytes(
                    database_credentials_document(password="changed")
                )
                + b"\n"
            )
            with self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "descriptor differs",
            ):
                MEDIA._inherited_database_credentials()

        with inherited_database_credentials(document) as (
            path,
            _descriptor,
            _payload,
        ):
            replacement = path.with_name("replacement.json")
            private_file(
                replacement,
                MEDIA.canonical_json_bytes(document) + b"\n",
            )
            os.replace(replacement, path)
            with self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "descriptor is unsafe|descriptor differs",
            ):
                MEDIA._inherited_database_credentials()

        with inherited_database_credentials(document):
            with mock.patch.dict(
                os.environ,
                {
                    MEDIA.DATABASE_CREDENTIALS_DIGEST_ENV: (
                        "sha256:" + "0" * 64
                    )
                },
                clear=False,
            ):
                with self.assertRaisesRegex(
                    MEDIA.MediaEvidenceError,
                    "descriptor differs",
                ):
                    MEDIA._inherited_database_credentials()

        with inherited_database_credentials(document) as (
            path,
            descriptor,
            _payload,
        ):
            swapped_path = path.with_name("fd-swap.json")
            private_file(
                swapped_path,
                MEDIA.canonical_json_bytes(
                    database_credentials_document(
                        password="fd-swap-secret"
                    )
                )
                + b"\n",
            )
            swapped_descriptor = os.open(swapped_path, os.O_RDONLY)
            try:
                os.dup2(swapped_descriptor, descriptor)
            finally:
                os.close(swapped_descriptor)
            with self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "descriptor differs",
            ):
                MEDIA._inherited_database_credentials()

    def test_secret_uses_only_stdin_pgpass_and_never_argv_env_or_error(
        self,
    ) -> None:
        password = r"rotated:password\\with-specials"
        system_identifier = "7612345678901234567"
        document = database_credentials_document(password=password)
        expected_pgpass = (
            r"127.0.0.1:*:*:*:rotated\:password\\\\with-specials"
        ).encode("utf-8")
        runner = self.RecordingRunner(
            system_identifier=system_identifier,
            accepted_pgpass=expected_pgpass,
        )
        with (
            inherited_database_credentials(document),
            self.stable_client_patches(),
        ):
            completed = MEDIA._run_trusted_psql(
                runner,
                container_id=CONTAINER_A,
                postgres_major=16,
                pgoptions=MEDIA.PSQL_AUDIT_PGOPTIONS,
                arguments=[
                    "-U",
                    "postgres",
                    "-d",
                    "postgres",
                    "-c",
                    "SELECT 1;",
                ],
                expected_image_id=IMAGE_ID,
            )
        self.assertEqual(completed.stdout, b"fixture-result\n")
        self.assertEqual(len(runner.commands), 2)
        self.assertTrue(
            all(
                expected_pgpass == payload.split(b"\n", 1)[0]
                for payload in runner.inputs
            )
        )
        for command, environment in zip(
            runner.commands,
            runner.environments,
            strict=True,
        ):
            rendered_command = "\0".join(command)
            rendered_environment = "\0".join(
                f"{key}={value}"
                for key, value in sorted(environment.items())
            )
            self.assertNotIn(password, rendered_command)
            self.assertNotIn(password, rendered_environment)
            self.assertNotIn("PGPASSWORD", environment)
            self.assertNotIn(
                MEDIA.DATABASE_CREDENTIALS_FD_ENV,
                environment,
            )
        self.assertNotIn(password, completed.stdout.decode())
        self.assertNotIn(password, completed.stderr.decode())

        wrong = database_credentials_document(password="wrong-secret")
        wrong_runner = self.RecordingRunner(
            accepted_pgpass=b"127.0.0.1:*:*:*:expected-secret",
        )
        with (
            inherited_database_credentials(wrong),
            self.stable_client_patches(),
        ):
            with self.assertRaises(MEDIA.MediaEvidenceError) as raised:
                MEDIA._run_trusted_psql(
                    wrong_runner,
                    container_id=CONTAINER_A,
                    postgres_major=16,
                    pgoptions=MEDIA.PSQL_AUDIT_PGOPTIONS,
                    arguments=[
                        "-U",
                        "postgres",
                        "-d",
                        "postgres",
                        "-c",
                        "SELECT 1;",
                    ],
                    expected_image_id=IMAGE_ID,
                )
        self.assertNotIn("wrong-secret", str(raised.exception))
        self.assertTrue(
            all(
                "wrong-secret" not in "\0".join(command)
                for command in wrong_runner.commands
            )
        )
        self.assertTrue(
            all(
                "wrong-secret"
                not in "\0".join(
                    f"{key}={value}"
                    for key, value in environment.items()
                )
                for environment in wrong_runner.environments
            )
        )

    def test_inherited_credential_rejects_trust_mode_before_client_start(
        self,
    ) -> None:
        runner = self.RecordingRunner()
        startup = trusted_startup_projection()
        with (
            inherited_database_credentials(
                database_credentials_document()
            ),
            mock.patch.object(
                MEDIA,
                "_trusted_server_runtime_epoch",
                return_value={"epoch": "stable"},
            ),
            mock.patch.object(
                MEDIA,
                "_local_audit_image_id",
                return_value=IMAGE_ID,
            ),
            mock.patch.object(
                MEDIA,
                "_trusted_server_startup_projection",
                return_value=startup,
            ),
            mock.patch.object(
                MEDIA,
                "_online_container_connection",
                return_value=("postgres", "postgres", True),
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "launcher rejects trust mode",
            ),
        ):
            MEDIA._run_trusted_psql(
                runner,
                container_id=CONTAINER_A,
                postgres_major=16,
                pgoptions=MEDIA.PSQL_AUDIT_PGOPTIONS,
                arguments=[
                    "-U",
                    "postgres",
                    "-d",
                    "postgres",
                    "-c",
                    "SELECT 1;",
                ],
                expected_image_id=IMAGE_ID,
            )
        self.assertEqual(runner.commands, [])

    def test_system_identifier_mismatch_stops_before_requested_sql(
        self,
    ) -> None:
        runner = self.RecordingRunner(
            system_identifier="7999999999999999999",
        )
        with (
            inherited_database_credentials(
                database_credentials_document()
            ),
            mock.patch.object(
                MEDIA,
                "_trusted_server_runtime_epoch",
                return_value={"epoch": "stable"},
            ),
            mock.patch.object(
                MEDIA,
                "_local_audit_image_id",
                return_value=IMAGE_ID,
            ),
            mock.patch.object(
                MEDIA,
                "_trusted_server_startup_projection",
                return_value=trusted_startup_projection(),
            ),
            mock.patch.object(
                MEDIA,
                "_online_container_connection",
                return_value=("postgres", "postgres", False),
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "system identity differs",
            ),
        ):
            MEDIA._run_trusted_psql(
                runner,
                container_id=CONTAINER_A,
                postgres_major=16,
                pgoptions=MEDIA.PSQL_AUDIT_PGOPTIONS,
                arguments=[
                    "-U",
                    "postgres",
                    "-d",
                    "postgres",
                    "-c",
                    "SELECT dangerous_mutation();",
                ],
                expected_image_id=IMAGE_ID,
            )
        self.assertEqual(len(runner.commands), 1)
        self.assertNotIn(
            "dangerous_mutation",
            "\0".join(runner.commands[0]),
        )

    def test_connection_argument_overrides_fail_before_client_start(
        self,
    ) -> None:
        hostile_arguments = (
            [
                "-U",
                "postgres",
                "-d",
                "dbname=postgres host=hostile",
            ],
            [
                "-U",
                "postgres",
                "-d",
                "postgres",
                "--host=hostile",
            ],
            [
                "-U",
                "postgres",
                "-d",
                "postgres",
                "--port=6543",
            ],
            [
                "-U",
                "postgres",
                "-d",
                "postgres",
                "--dbname=postgresql://hostile/postgres",
            ],
        )
        for arguments in hostile_arguments:
            with self.subTest(arguments=arguments):
                runner = self.RecordingRunner()
                with (
                    mock.patch.object(
                        MEDIA,
                        "_trusted_server_runtime_epoch",
                        return_value={"epoch": "stable"},
                    ),
                    mock.patch.object(
                        MEDIA,
                        "_local_audit_image_id",
                        return_value=IMAGE_ID,
                    ),
                    mock.patch.object(
                        MEDIA,
                        "_trusted_server_startup_projection",
                        return_value=trusted_startup_projection(),
                    ),
                    mock.patch.object(
                        MEDIA,
                        "_online_container_connection",
                        return_value=("postgres", "postgres", True),
                    ),
                    self.assertRaisesRegex(
                        MEDIA.MediaEvidenceError,
                        (
                            "identity differs|"
                            "arguments override connection authority"
                        ),
                    ),
                ):
                    MEDIA._run_trusted_psql(
                        runner,
                        container_id=CONTAINER_A,
                        postgres_major=16,
                        pgoptions=MEDIA.PSQL_AUDIT_PGOPTIONS,
                        arguments=arguments,
                        expected_image_id=IMAGE_ID,
                    )
                self.assertEqual(runner.commands, [])


class MediaEvidenceLauncherTests(unittest.TestCase):
    def test_real_implementation_accepts_only_its_pinned_inherited_fd(
        self,
    ) -> None:
        source = ROOT / "scripts/postgres_media_evidence.py"
        with tempfile.TemporaryDirectory(
            prefix="postgres-media-installed-"
        ) as raw:
            implementation = Path(raw) / source.name
            implementation.write_bytes(source.read_bytes())
            os.chmod(implementation, 0o700)
            expected = MEDIA.sha256_bytes(implementation.read_bytes())
            descriptor = os.open(implementation, os.O_RDONLY)
            try:
                with (
                    mock.patch.object(
                        MEDIA,
                        "__file__",
                        f"/proc/self/fd/{descriptor}",
                    ),
                    mock.patch.dict(
                        os.environ,
                        {
                            "NEXPOLY_MEDIA_AUDITOR_SHA256": expected,
                            "NEXPOLY_ACTIVE_CONTROL_RELEASE_ID": "a" * 64,
                        },
                        clear=False,
                    ),
                ):
                    self.assertEqual(MEDIA._auditor_digest(), expected)
                    os.environ["NEXPOLY_MEDIA_AUDITOR_SHA256"] = (
                        "sha256:" + "f" * 64
                    )
                    with self.assertRaisesRegex(
                        MEDIA.MediaEvidenceError,
                        "identity differs",
                    ):
                        MEDIA._auditor_digest()
            finally:
                os.close(descriptor)

            os.chmod(implementation, 0o755)
            descriptor = os.open(implementation, os.O_RDONLY)
            try:
                with (
                    mock.patch.object(
                        MEDIA,
                        "__file__",
                        f"/proc/self/fd/{descriptor}",
                    ),
                    mock.patch.dict(
                        os.environ,
                        {
                            "NEXPOLY_MEDIA_AUDITOR_SHA256": expected,
                            "NEXPOLY_ACTIVE_CONTROL_RELEASE_ID": "a" * 64,
                        },
                        clear=False,
                    ),
                    self.assertRaisesRegex(
                        MEDIA.MediaEvidenceError,
                        "inherited implementation identity differs",
                    ),
                ):
                    MEDIA._auditor_digest()
            finally:
                os.close(descriptor)

    def test_wrapper_launcher_chain_preserves_only_the_pinned_rules_digest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="postgres-media-launcher-"
        ) as raw:
            root = Path(raw)
            runtime = root / "runtime"
            config = runtime / "config"
            binary = runtime / "bin"
            releases = runtime / "control-releases"
            state = runtime / "state"
            config.mkdir(parents=True, mode=0o700)
            binary.mkdir(parents=True, mode=0o700)
            releases.mkdir(parents=True, mode=0o700)
            state.mkdir(parents=True, mode=0o700)
            rules = config / "postgres-media-authority-rules.json"
            private_file(rules, b'{"schema_version":1}\n')
            authority_digest = MEDIA.sha256_bytes(rules.read_bytes())
            role_payload = b"-- reviewed role fixture\n"
            role_digest = MEDIA.sha256_bytes(role_payload)
            credential_secret = "launcher-fixed-secret"
            credential_payload = (
                MEDIA.canonical_json_bytes(
                    database_credentials_document(
                        password=credential_secret,
                    )
                )
                + b"\n"
            )
            credentials = config / "postgres-media-credentials.json"
            private_file(credentials, credential_payload)
            credentials_digest = MEDIA.sha256_bytes(
                credential_payload
            )

            stable = binary / "nexpoly-postgres-media-evidence"
            stable.write_bytes(
                (ROOT / "scripts/nexpoly-postgres-media-evidence").read_bytes()
            )
            os.chmod(stable, 0o700)
            selector = binary / "control_runtime_selector.py"
            selector.write_bytes(
                (ROOT / "scripts/control_runtime_selector.py").read_bytes()
            )
            os.chmod(selector, 0o700)
            immutable: dict[str, str] = {}
            for name in MEDIA.BOOTSTRAP_IMMUTABLE_FILES:
                path = binary / name
                if not path.exists():
                    path.write_text(f"fixture {name}\n", encoding="utf-8")
                    os.chmod(path, 0o700)
                immutable[name] = MEDIA.sha256_bytes(path.read_bytes())

            implementation_payload = "\n".join(
                (
                    "import json",
                    "import os",
                    "import sys",
                    "print(json.dumps({",
                    "  'argv': sys.argv[1:],",
                    "  'authority': os.environ.get(",
                    "    'NEXPOLY_CONTRACT_0012_MEDIA_AUTHORITY_RULES_SHA256'",
                    "  ),",
                    "  'role_sql': os.environ.get(",
                    "    'NEXPOLY_MEDIA_AUDIT_ROLE_SQL_SHA256'",
                    "  ),",
                    "  'implementation': os.environ.get(",
                    "    'NEXPOLY_MEDIA_AUDITOR_SHA256'",
                    "  ),",
                    "  'control_release': os.environ.get(",
                    "    'NEXPOLY_ACTIVE_CONTROL_RELEASE_ID'",
                    "  ),",
                    "  'credential_digest': os.environ.get(",
                    "    'NEXPOLY_MEDIA_DATABASE_CREDENTIALS_SHA256'",
                    "  ),",
                    "  'credential_fd_valid': os.fstat(int(",
                    "    os.environ['NEXPOLY_MEDIA_DATABASE_CREDENTIALS_FD']",
                    "  )).st_size > 0,",
                    "  'ambient_secret': os.environ.get('AMBIENT_SECRET'),",
                    "}, sort_keys=True))",
                    "",
                )
            ).encode()
            launcher_payload = (
                ROOT / "scripts/postgres_media_launcher.py"
            ).read_bytes()
            payloads = {
                "deploy.py": b"# sealed deploy fixture\n",
                "postgres_media_launcher.py": launcher_payload,
                "postgres_media_evidence.py": implementation_payload,
                "postgres-media-authority-rules.json": rules.read_bytes(),
                "postgres-media-audit-role.sql.example": role_payload,
            }
            files = {
                name: {
                    "sha256": MEDIA.sha256_bytes(payload),
                    "size": len(payload),
                    "mode": 0o700,
                }
                for name, payload in payloads.items()
            }
            identity = {
                "schema_version": 1,
                "protocol_version": 1,
                "source_sha": "1" * 40,
                "source_tree": "2" * 40,
                "compatibility": {
                    "handoff_protocol_versions": [1],
                    "descriptor_schema_versions": [2, 3],
                    "current_state_schema_versions": [2],
                    "marker_schema_versions": [2],
                    "worker_slot_schema_versions": [2],
                    "prepare_abort_abi_versions": [1],
                },
                "entrypoints": {
                    "deploy": {
                        "kind": "python",
                        "file": "deploy.py",
                    },
                    "postgres-media-evidence": {
                        "kind": "python",
                        "file": "postgres_media_launcher.py",
                    }
                },
                "files": files,
            }
            release_id = MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(identity)
            ).removeprefix("sha256:")
            manifest = {**identity, "release_id": release_id}
            release = releases / release_id
            release.mkdir(mode=0o700)
            for name, payload in payloads.items():
                path = release / name
                path.write_bytes(payload)
                os.chmod(path, 0o700)
            manifest_path = release / "CONTROL-MANIFEST.json"
            private_file(
                manifest_path,
                MEDIA.canonical_json_bytes(manifest) + b"\n",
            )
            manifest_digest = MEDIA.sha256_bytes(
                manifest_path.read_bytes()
            )
            candidate = {
                "schema_version": 1,
                "protocol_version": 1,
                "component": "deployment-controls",
                "release_id": release_id,
                "source_sha": identity["source_sha"],
                "source_tree": identity["source_tree"],
                "manifest_sha256": manifest_digest,
                "operation_id": "bootstrap-media-fixture",
                "prepared_at": "2026-07-17T00:00:00+00:00",
            }
            active = {
                "schema_version": 1,
                "protocol_version": 1,
                "component": "deployment-controls",
                "generation": 1,
                "release_id": release_id,
                "source_sha": identity["source_sha"],
                "source_tree": identity["source_tree"],
                "manifest_sha256": manifest_digest,
                "operation_id": "bootstrap-media-fixture",
                "previous_release_id": None,
                "activated_at": "2026-07-17T00:00:00+00:00",
            }
            private_file(
                state / "active-control.json",
                MEDIA.canonical_json_bytes(active) + b"\n",
            )
            readiness = {
                "schema_version": 2,
                "ready": True,
                "source_root": str(root / "bootstrap-source"),
                "source_sha": identity["source_sha"],
                "source_tree": identity["source_tree"],
                "branch": "main",
                "origin": "git@github.com:lzq390/ZhijuPoly.git",
                "remote_names": ["origin"],
                "origin_fetch_urls": [
                    "git@github.com:lzq390/ZhijuPoly.git"
                ],
                "origin_push_urls": [
                    "git@github.com:lzq390/ZhijuPoly.git"
                ],
                "origin_main_sha": identity["source_sha"],
                "standalone_object_database": True,
                "shallow": False,
                "dirty_entries": 0,
                "ignored_entries": 0,
                "unreachable_objects": 0,
                "replace_refs": 0,
                "special_index_entries": 0,
                "sparse_index": False,
                "owner_private": True,
                "group_or_world_writable": False,
            }
            takeover = {
                "schema_version": 1,
                "operation_id": "takeover-media-fixture",
                "authority_sha": identity["source_sha"],
                "authority_tree": identity["source_tree"],
                "install_manifest_sha256": "sha256:" + "3" * 64,
                "classification_sha256": "sha256:" + "4" * 64,
                "runtime_identity_sha256": "sha256:" + "5" * 64,
                "git_identity": {
                    "branch": "refs/heads/main",
                    "head_sha": "0" * 40,
                    "head_tree": "0" * 40,
                    "local_main_sha": "0" * 40,
                },
                "pre_stopped_fence_sha256": "sha256:" + "6" * 64,
                "control_layout_sha256": "sha256:" + "7" * 64,
                "checkout_permissions_sha256": "sha256:" + "8" * 64,
                "applied_record_sha256": "sha256:" + "9" * 64,
            }
            takeover["binding_sha256"] = MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(takeover)
            )
            private_file(
                state / "bootstrap-control.json",
                MEDIA.canonical_json_bytes(
                    {
                        "schema_version": 2,
                        "status": "completed",
                        "source_sha": identity["source_sha"],
                        "source_tree": identity["source_tree"],
                        "source_readiness": readiness,
                        "source_readiness_sha256": (
                            MEDIA.sha256_bytes(
                                MEDIA.canonical_json_bytes(readiness)
                            )
                        ),
                        "legacy_takeover": takeover,
                        "delivery_gate": {"fixture": True},
                        "production_repository": {"fixture": True},
                        "immutable_files": immutable,
                        "worker_unit_takeover": {"fixture": True},
                        "candidate_control": candidate,
                        "active_control": active,
                    }
                )
                + b"\n",
            )

            launcher_digest = files["postgres_media_launcher.py"][
                "sha256"
            ]
            implementation_digest = files["postgres_media_evidence.py"][
                "sha256"
            ]
            environment = {
                "PATH": "/usr/bin:/bin",
                (
                    "NEXPOLY_CONTRACT_0012_"
                    "MEDIA_AUTHORITY_RULES_SHA256"
                ): authority_digest,
                "NEXPOLY_CONTRACT_0012_AUDIT_ROLE_SQL_SHA256": (
                    role_digest
                ),
                "NEXPOLY_MEDIA_LAUNCHER_SHA256": launcher_digest,
                "NEXPOLY_MEDIA_IMPLEMENTATION_SHA256": (
                    implementation_digest
                ),
                "AMBIENT_SECRET": "must-not-cross-execve",
                "NEXPOLY_MEDIA_DATABASE_CREDENTIALS_FD": "999999",
                "NEXPOLY_MEDIA_DATABASE_CREDENTIALS_SHA256": (
                    "sha256:" + "0" * 64
                ),
            }

            result = subprocess.run(
                [str(stable)],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(result.stdout)
            self.assertEqual(observed["argv"], ["build"])
            self.assertEqual(observed["authority"], authority_digest)
            self.assertEqual(observed["role_sql"], role_digest)
            self.assertEqual(
                observed["implementation"],
                implementation_digest,
            )
            self.assertEqual(observed["control_release"], release_id)
            self.assertEqual(
                observed["credential_digest"],
                credentials_digest,
            )
            self.assertTrue(observed["credential_fd_valid"])
            self.assertIsNone(observed["ambient_secret"])
            self.assertNotIn(credential_secret, result.stdout)
            self.assertNotIn(credential_secret, result.stderr)

            operator_result = subprocess.run(
                [str(stable), "role-plan"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    "PATH": "/usr/bin:/bin",
                    "AMBIENT_SECRET": "must-not-cross-execve",
                },
            )
            self.assertEqual(
                operator_result.returncode,
                0,
                operator_result.stderr,
            )
            operator_observed = json.loads(operator_result.stdout)
            self.assertEqual(operator_observed["argv"], ["role-plan"])
            self.assertEqual(
                operator_observed["authority"],
                authority_digest,
            )
            self.assertEqual(
                operator_observed["role_sql"],
                role_digest,
            )
            self.assertEqual(
                operator_observed["implementation"],
                implementation_digest,
            )
            self.assertIsNone(operator_observed["ambient_secret"])

            mismatched_environment = {
                **environment,
                (
                    "NEXPOLY_CONTRACT_0012_"
                    "MEDIA_AUTHORITY_RULES_SHA256"
                ): "sha256:" + "0" * 64,
            }
            mismatched = subprocess.run(
                [str(stable), "role-plan"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=mismatched_environment,
            )
            self.assertNotEqual(mismatched.returncode, 0)

            revalidated = subprocess.run(
                [str(stable), "revalidate"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            self.assertEqual(
                json.loads(revalidated.stdout)["argv"],
                ["revalidate"],
            )
            foreign_mode = subprocess.run(
                [str(stable), "foreign-mode"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            self.assertNotEqual(foreign_mode.returncode, 0)

            os.chmod(credentials, 0o640)
            unsafe_credentials = subprocess.run(
                [str(stable), "revalidate"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            self.assertNotEqual(unsafe_credentials.returncode, 0)
            self.assertNotIn(
                credential_secret,
                unsafe_credentials.stderr,
            )
            os.chmod(credentials, 0o600)

            held_credentials = config / ".held-postgres-media-credentials"
            os.replace(credentials, held_credentials)
            missing_credentials = subprocess.run(
                [str(stable), "revalidate"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            self.assertNotEqual(missing_credentials.returncode, 0)
            self.assertNotIn(
                credential_secret,
                missing_credentials.stderr,
            )
            os.replace(held_credentials, credentials)

            b_identity = {
                **identity,
                "source_sha": "3" * 40,
                "source_tree": "4" * 40,
            }
            b_release_id = MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(b_identity)
            ).removeprefix("sha256:")
            b_manifest = {
                **b_identity,
                "release_id": b_release_id,
            }
            b_release = releases / b_release_id
            b_release.mkdir(mode=0o700)
            for name, payload in payloads.items():
                path = b_release / name
                path.write_bytes(payload)
                os.chmod(path, 0o700)
            b_manifest_path = b_release / "CONTROL-MANIFEST.json"
            private_file(
                b_manifest_path,
                MEDIA.canonical_json_bytes(b_manifest) + b"\n",
            )
            b_active = {
                **active,
                "generation": 2,
                "release_id": b_release_id,
                "source_sha": b_identity["source_sha"],
                "source_tree": b_identity["source_tree"],
                "manifest_sha256": MEDIA.sha256_bytes(
                    b_manifest_path.read_bytes()
                ),
                "operation_id": "deploy-media-bridge-b",
                "previous_release_id": release_id,
            }
            private_file(
                state / "active-control.json",
                MEDIA.canonical_json_bytes(b_active) + b"\n",
            )
            through_b = subprocess.run(
                [str(stable), "revalidate"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            self.assertEqual(through_b.returncode, 0, through_b.stderr)
            self.assertEqual(
                json.loads(through_b.stdout)["control_release"],
                b_release_id,
            )

            drift_payloads = {
                **payloads,
                "postgres-media-audit-role.sql.example": (
                    b"-- unapproved active role contract\n"
                ),
            }
            drift_files = {
                name: {
                    "sha256": MEDIA.sha256_bytes(payload),
                    "size": len(payload),
                    "mode": 0o700,
                }
                for name, payload in drift_payloads.items()
            }
            drift_identity = {
                **identity,
                "source_sha": "5" * 40,
                "source_tree": "6" * 40,
                "files": drift_files,
            }
            drift_release_id = MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(drift_identity)
            ).removeprefix("sha256:")
            drift_manifest = {
                **drift_identity,
                "release_id": drift_release_id,
            }
            drift_release = releases / drift_release_id
            drift_release.mkdir(mode=0o700)
            for name, payload in drift_payloads.items():
                path = drift_release / name
                path.write_bytes(payload)
                os.chmod(path, 0o700)
            drift_manifest_path = drift_release / "CONTROL-MANIFEST.json"
            private_file(
                drift_manifest_path,
                MEDIA.canonical_json_bytes(drift_manifest) + b"\n",
            )
            drift_active = {
                **active,
                "generation": 3,
                "release_id": drift_release_id,
                "source_sha": drift_identity["source_sha"],
                "source_tree": drift_identity["source_tree"],
                "manifest_sha256": MEDIA.sha256_bytes(
                    drift_manifest_path.read_bytes()
                ),
                "operation_id": "deploy-media-unapproved-active",
                "previous_release_id": b_release_id,
            }
            private_file(
                state / "active-control.json",
                MEDIA.canonical_json_bytes(drift_active) + b"\n",
            )
            drifted = subprocess.run(
                [str(stable), "role-plan"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin"},
            )
            self.assertNotEqual(drifted.returncode, 0)
            self.assertIn(
                "bootstrap F authority",
                drifted.stderr,
            )

            reactivated_f = {
                **active,
                "generation": 4,
                "operation_id": "deploy-media-final-f",
                "previous_release_id": drift_release_id,
            }
            private_file(
                state / "active-control.json",
                MEDIA.canonical_json_bytes(reactivated_f) + b"\n",
            )
            through_f = subprocess.run(
                [str(stable), "revalidate"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            self.assertEqual(through_f.returncode, 0, through_f.stderr)
            self.assertEqual(
                json.loads(through_f.stdout)["control_release"],
                release_id,
            )

            sentinel = root / "malicious-sentinel"
            implementation = release / "postgres_media_evidence.py"
            implementation.write_text(
                "\n".join(
                    (
                        "from pathlib import Path",
                        f"Path({str(sentinel)!r}).write_text('executed')",
                    )
                ),
                encoding="utf-8",
            )
            os.chmod(implementation, 0o700)
            rejected = subprocess.run(
                [str(stable)],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(sentinel.exists())


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
            "docker-volume:nexpoly_app_postgres_data",
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

    def test_unsafe_locator_characters_use_stable_hashed_media_ids(self) -> None:
        uppercase_volume = "ByteFF2_Cache"
        spaced_bind = "/data/lzq/gith/nexpoly/Model Assets/ByteFF2"
        volume_id = MEDIA.media_id_for_locator(
            "docker_volume",
            uppercase_volume,
        )
        bind_id = MEDIA.media_id_for_locator(
            "container_bind",
            spaced_bind,
        )
        self.assertRegex(volume_id, MEDIA.MEDIA_ID_RE)
        self.assertRegex(bind_id, MEDIA.MEDIA_ID_RE)
        self.assertTrue(volume_id.startswith("docker-volume-sha256:"))
        self.assertTrue(bind_id.startswith("container-bind-sha256:"))
        self.assertEqual(
            volume_id,
            CONTRACTS.media_id_for_locator(
                "docker_volume",
                uppercase_volume,
            ),
        )
        self.assertEqual(
            bind_id,
            CONTRACTS.media_id_for_locator(
                "container_bind",
                spaced_bind,
            ),
        )
        validated = CONTRACTS._external_source_identity_v3(
            {
                "path": spaced_bind,
                "device": 1,
                "inode": 2,
                "mtime_ns": 3,
                "ctime_ns": 4,
                "mode": 0o700,
                "uid": os.geteuid(),
                "data_subpath": ".",
                "attached": [],
            },
            kind="container_bind",
            media_id=bind_id,
        )
        self.assertEqual(validated["path"], spaced_bind)

    def test_immutable_publish_recovers_before_and_after_rename_crash(
        self,
    ) -> None:
        evidence = self.root / "atomic-evidence"
        private_directory(evidence)
        implementation = ROOT / "scripts/postgres_media_evidence.py"
        payload = b'{"fixture":true}\n'
        program = r"""
import importlib.util
import os
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("crash_media", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
real_rename = module.os.rename
mode = sys.argv[4]
def checkpoint(*args, **kwargs):
    if mode == "after":
        real_rename(*args, **kwargs)
    os._exit(91)
module.os.rename = checkpoint
module._write_private_atomic(
    Path(sys.argv[2]),
    sys.argv[3],
    b'{"fixture":true}\n',
)
"""
        for mode in ("before", "after"):
            name = f"{mode}.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    program,
                    str(implementation),
                    str(evidence),
                    name,
                    mode,
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(completed.returncode, 91)
            published = MEDIA._write_private_atomic(
                evidence,
                name,
                payload,
            )
            self.assertEqual(published.read_bytes(), payload)
            self.assertEqual(
                [
                    candidate.name
                    for candidate in evidence.iterdir()
                    if candidate.name.startswith(f".{name}.tmp-")
                ],
                [],
            )
            self.assertEqual(published.stat().st_nlink, 1)

    def test_tracked_authority_rules_are_static_and_pin_all_audit_images(
        self,
    ) -> None:
        source = ROOT / "ops/config/postgres-media-authority-rules.json"
        installed = self.config / "postgres-media-authority-rules.json"
        private_file(installed, source.read_bytes())
        loaded = MEDIA.load_authority_rules(
            installed,
            policy=MEDIA.DiscoveryPolicy(),
            private_root=self.config,
        )
        document = json.loads(source.read_text(encoding="utf-8"))
        self.assertEqual(
            loaded.digest,
            MEDIA.sha256_bytes(source.read_bytes()),
        )
        self.assertEqual(
            dict(loaded.audit_images),
            MEDIA.POSTGRES_AUDIT_IMAGES,
        )
        self.assertNotIn("expected_media", document)
        self.assertNotIn("required_online_databases", document)
        roles = {
            rule["stack"]: rule["audit_role"]
            for rule in document["logical_media"]["named_stacks"]
        }
        self.assertEqual(
            roles,
            {
                "nexpoly_dev": "nexpoly_dev_auditor",
                "nexpoly_md_health_opt": "nexpoly_health_auditor",
            },
        )
        deploy_example = (
            ROOT / "ops/config/deploy.env.example"
        ).read_text(encoding="utf-8")
        for role in roles.values():
            self.assertIn(f"={role}", deploy_example)
        for rule in document["logical_media"]["named_stacks"]:
            self.assertEqual(
                set(rule),
                {
                    "stack",
                    "volume_name_pattern",
                    "database",
                    "audit_role",
                    "online_service",
                    "allowed_state",
                },
            )
            self.assertNotIn("media_id", rule)
            self.assertNotIn("oid", rule)
            self.assertNotIn("owner", rule)

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
        development = next(
            record
            for record in value["expected_media"]
            if record["database"] == "nexpoly_dev"
        )
        development.update(
            {
                "database_user": "nexpoly_dev",
                "disposition": "retained-private-isolated",
                "audit_method": "isolated-volume-copy-read-only",
                "online_admin_role": None,
            }
        )
        development["databases"][0].update(
            {"owner": "nexpoly_dev", "audit_role": "nexpoly_dev"}
        )
        health = next(
            record
            for record in value["expected_media"]
            if record["database"] == "nexpoly_md_health_opt"
        )
        health.update(
            {
                "database_user": "postgres",
                "disposition": "retained-private-isolated",
                "audit_method": "isolated-volume-copy-read-only",
                "online_admin_role": None,
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
                "online_admin_role": "polyprop",
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
                "online_admin_role": "polyprop",
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

    def test_pg18_requires_full_isolated_inventory_and_live_pg18_is_rejected(
        self,
    ) -> None:
        value = self.valid_document()
        pg18 = descriptor(
            "docker-volume:d-adjacent-pg18",
            "postgres",
            disposition="retained-private-isolated",
            method="isolated-volume-copy-read-only",
            user="postgres",
            service=None,
            source_postgres_major=18,
        )
        value["expected_media"].append(pg18)
        value["expected_media"].sort(key=lambda record: record["media_id"])
        self.write_registry(value)
        loaded = MEDIA.load_registry(
            self.registry,
            policy=self.policy,
            private_root=self.config,
        )
        self.assertEqual(
            next(
                record
                for record in loaded.descriptors
                if record.media_id == pg18["media_id"]
            ).source_postgres_major,
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
        record_only = {
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
        value["expected_media"].append(record_only)
        value["expected_media"].sort(key=lambda record: record["media_id"])
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
        next(
            record
            for record in value["expected_media"]
            if record["database"] == "nexpoly_dev"
        )["source_postgres_major"] = 18
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

    def test_every_postgres_backup_requires_full_isolated_restore_inventory(
        self,
    ) -> None:
        value = self.valid_document()
        backup = self.backup_a / "postgres.dump"
        adjacent = {
            **descriptor(
                f"postgres-backup:{backup}",
                "nexpoly",
                disposition="retained-private-isolated",
                method="isolated-backup-restore-read-only",
                user="postgres",
                service=None,
            ),
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
        self.assertEqual(len(selected.databases), 1)
        self.assertEqual(selected.databases[0]["name"], "nexpoly")

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


class RuntimeRegistryGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="postgres-media-runtime-registry-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.config = self.root / "config"
        self.reviewed = self.config / "reviewed-content"
        self.evidence = self.root / "evidence"
        self.workspace = self.evidence / "workspace"
        for path in (
            self.config,
            self.reviewed,
            self.evidence,
            self.workspace,
        ):
            private_directory(path)
        self.backup_a = self.root / "backup-a"
        self.backup_b = self.root / "backup-b"
        for path in (self.backup_a, self.backup_b):
            private_directory(path)
        self.policy = MEDIA.DiscoveryPolicy(
            backup_roots=(self.backup_a, self.backup_b)
        )
        self.authority = MEDIA.MediaAuthorityRules(
            payload=b"fixture-authority",
            digest="sha256:" + "8" * 64,
            audit_image=MEDIA.POSTGRES_AUDIT_IMAGES[16],
            auditor_sha256=MEDIA._auditor_digest(),
            descriptors=(),
            required_online_databases=(),
            policy=self.policy,
            allow_unmatched_non_postgres=False,
            production_identity={
                "stack": "production",
                "database": "nexpoly",
                "kind": "docker_volume",
                "media_id": "docker-volume:nexpoly_app_postgres_data",
                "postgres_major": 16,
                "system_identifier": "7659245354718314530",
            },
            audit_images=tuple(
                sorted(MEDIA.POSTGRES_AUDIT_IMAGES.items())
            ),
            logical_media=copy.deepcopy(MEDIA.LOGICAL_MEDIA_POLICY),
        )
        self.operation = mock.Mock()
        self.operation.workspace = self.workspace
        self.operation.authority = {
            "postgres_images": {
                str(major): {
                    "digest_ref": image,
                    "image_id": "sha256:" + f"{major:02x}" * 32,
                }
                for major, image in sorted(
                    MEDIA.POSTGRES_AUDIT_IMAGES.items()
                )
            }
        }
        self.registry_path = self.config / "postgres-media-registry.json"
        self.discovery = self._discovery()
        self.derive_calls: list[tuple[str, int | None, str]] = []

    @staticmethod
    def _source(
        name: str,
        *,
        signature: str = "postgres",
        major: int | None = 16,
        active: bool = False,
    ) -> MEDIA.DiscoveredMedia:
        classification_probe = (
            {
                "method": "bounded-postgres-marker-probe-v1",
                "maximum_entries": (
                    MEDIA.MAX_POSTGRES_MARKER_PROBE_ENTRIES
                ),
                "maximum_marker_results": (
                    MEDIA.MAX_POSTGRES_MARKER_RESULTS
                ),
                "timeout_seconds": (
                    MEDIA.POSTGRES_MARKER_PROBE_TIMEOUT_SECONDS
                ),
                "content_read_scope": "PG_VERSION-up-to-32-bytes",
                "result": "no-postgres-markers",
            }
            if signature == "non-postgres"
            else None
        )
        return MEDIA.DiscoveredMedia(
            media_id=f"docker-volume:{name}",
            kind="docker_volume",
            locator=name,
            data_subpath="." if signature == "non-postgres" else "pgdata",
            attached=(
                (attachment_record(CONTAINER_A),)
                if active
                else ()
            ),
            signature=signature,
            postgres_major=major,
            classification_probe=classification_probe,
            docker_inspect_sha256=(
                "sha256:" + "5" * 64
                if signature == "non-postgres"
                else None
            ),
        )

    def _discovery(self, *, docker_digest: str | None = None):
        sources = [
            self._source(
                "nexpoly_app_postgres_data",
                active=True,
            ),
            self._source(
                "nexpoly_dev_nexpoly_dev_postgres_data",
            ),
            self._source(
                "nexpoly_md_health_opt_app_postgres_data",
            ),
            self._source("research_pg18", major=18),
            self._source(
                "model-cache",
                signature="non-postgres",
                major=None,
            ),
        ]
        return MEDIA.Discovery(
            media={source.media_id: source for source in sources},
            docker_inventory_sha256=(
                docker_digest or "sha256:" + "1" * 64
            ),
            backup_inventory_sha256="sha256:" + "2" * 64,
            scanned_volume_names=tuple(
                sorted(source.locator for source in sources)
            ),
            scanned_bind_sources=(),
            scanned_container_ids=(CONTAINER_A,),
        )

    @staticmethod
    def _database_record(
        name: str,
        *,
        oid: str,
        owner: str,
        audit_role: str,
    ) -> dict[str, object]:
        return {
            "name": name,
            "oid": oid,
            "owner": owner,
            "allow_connections": True,
            "template": False,
            "audit_role": audit_role,
            "migration_scope": "nexpoly-ledger",
        }

    def _live_descriptor(
        self,
        _authority,
        source,
        *,
        primary_database,
        audit_role,
        disposition,
        runner,
        audit_image_id,
    ):
        del runner, audit_image_id
        return MEDIA.MediaDescriptor(
            media_id=source.media_id,
            kind="docker_volume",
            database=primary_database,
            database_user=audit_role,
            disposition=disposition,
            audit_method="live-read-only",
            online_admin_role=audit_role,
            classification="nexpoly-db",
            source_postgres_major=16,
            databases=(
                self._database_record(
                    primary_database,
                    oid="16384",
                    owner=primary_database,
                    audit_role=audit_role,
                ),
            ),
        )

    def _isolated_descriptor(
        self,
        _authority,
        source,
        *,
        primary_database,
        runner,
        operation,
        checkpoint_sink=None,
    ):
        del runner, operation
        self.derive_calls.append(
            (source.media_id, source.postgres_major, primary_database)
        )
        oid = "18042" if source.postgres_major == 18 else "16420"
        owner = (
            "pg18_owner"
            if source.postgres_major == 18
            else primary_database
        )
        descriptor = MEDIA.MediaDescriptor(
            media_id=source.media_id,
            kind="docker_volume",
            database=primary_database,
            database_user="postgres",
            disposition="retained-private-isolated",
            audit_method="isolated-volume-copy-read-only",
            classification="nexpoly-db",
            source_postgres_major=source.postgres_major,
            databases=(
                self._database_record(
                    primary_database,
                    oid=oid,
                    owner=owner,
                    audit_role="postgres",
                ),
            ),
        )
        if checkpoint_sink is not None:
            identity = {
                "name": source.locator,
                "data_subpath": source.data_subpath,
                "attached": [],
            }
            checkpoint_sink(
                source.media_id,
                {
                    "schema_version": 1,
                    "media_id": source.media_id,
                    "source_document_sha256": MEDIA.sha256_bytes(
                        MEDIA.canonical_json_bytes(source.document())
                    ),
                    "descriptor": descriptor.document(),
                    "descriptor_sha256": MEDIA.sha256_bytes(
                        MEDIA.canonical_json_bytes(
                            descriptor.document()
                        )
                    ),
                    "method": descriptor.audit_method,
                    "database": {
                        "databases": [
                            {
                                **dict(descriptor.databases[0]),
                                "audit_state": "complete",
                                "audit": {},
                            }
                        ]
                    },
                    "source_content_sha256": "sha256:" + "a" * 64,
                    "source_identity_before": identity,
                    "source_identity_after": dict(identity),
                    "isolation": {
                        "source_mounted_read_only": True,
                        "source_started_as_postgres": False,
                        "scratch_network": "none",
                        "scratch_destroyed": True,
                        "copy_method": (
                            "readonly-tar-copy-to-disposable-volume-v1"
                        ),
                    },
                    "scope": "copied-source-cluster",
                    "algorithm": (
                        "postgres-data-directory-tar-sha256-v1"
                    ),
                },
            )
        return descriptor

    def _backup_descriptor(
        self,
        _authority,
        source,
        *,
        runner,
        operation,
        checkpoint_sink=None,
    ):
        del runner, operation, checkpoint_sink
        return MEDIA.MediaDescriptor(
            media_id=source.media_id,
            kind="postgres_backup",
            database="nexpoly",
            database_user="postgres",
            disposition="retained-private-isolated",
            audit_method="isolated-backup-restore-read-only",
            classification="nexpoly-db",
            source_postgres_major=None,
            databases=(
                self._database_record(
                    "nexpoly",
                    oid="16421",
                    owner="postgres",
                    audit_role="postgres",
                ),
            ),
        )

    @staticmethod
    def _review(_registry, source, **_kwargs):
        return {
            "media_id": source.media_id,
            "source_identity_sha256": "sha256:" + "3" * 64,
            "source_content_sha256": "sha256:" + "4" * 64,
            "file_count": 7,
            "size_bytes": 4096,
            "review_algorithm": (
                "private-reviewed-content-inventory-v1"
            ),
        }

    def _generate(self, discoveries):
        expected_revalidation = (
            discoveries[1] if len(discoveries) > 1 else discoveries[0]
        )

        def revalidate_docker(_runner, original):
            if (
                expected_revalidation.docker_inventory_sha256
                != original.docker_inventory_sha256
                or expected_revalidation.scanned_volume_names
                != original.scanned_volume_names
                or expected_revalidation.scanned_bind_sources
                != original.scanned_bind_sources
                or expected_revalidation.scanned_container_ids
                != original.scanned_container_ids
            ):
                raise MEDIA.MediaEvidenceError(
                    "Docker media changed before publishing registry"
                )

        with (
            mock.patch.object(
                MEDIA,
                "discover_media",
                side_effect=[discoveries[0]],
            ) as discover,
            mock.patch.object(
                MEDIA,
                "_live_source_system_identifier",
                return_value="7659245354718314530",
            ),
            mock.patch.object(
                MEDIA,
                "_live_runtime_descriptor",
                side_effect=self._live_descriptor,
            ),
            mock.patch.object(
                MEDIA,
                "_derive_isolated_volume_descriptor",
                side_effect=self._isolated_descriptor,
            ),
            mock.patch.object(
                MEDIA,
                "_derive_isolated_backup_descriptor",
                side_effect=self._backup_descriptor,
            ),
            mock.patch.object(
                MEDIA,
                "_retained_source_admin_role",
                return_value="postgres",
            ),
            mock.patch.object(
                MEDIA,
                "_review_non_postgres_volume",
                side_effect=self._review,
            ),
            mock.patch.object(
                MEDIA,
                "_revalidate_docker_epoch",
                side_effect=revalidate_docker,
            ),
            mock.patch.object(
                MEDIA,
                "_revalidate_backup_epoch",
            ),
            mock.patch.object(
                MEDIA,
                "_revalidate_live_registry_epoch",
            ),
        ):
            result = MEDIA.generate_runtime_registry(
                self.authority,
                registry_path=self.registry_path,
                runner=mock.Mock(),
                operation=self.operation,
                reviewed_content_root=self.reviewed,
            )
        return result, discover

    def test_release_backup_sidecars_and_secret_file_are_stream_reviewed(
        self,
    ) -> None:
        dump = self.backup_a / "release.dump"
        metadata = self.backup_a / "release.dump.json"
        checksum = self.backup_a / "release.dump.sha256"
        secret = self.backup_a / ".env.bak-20260718"
        secret_payload = b"POSTGRES_PASSWORD=do-not-publish-this-value\n"
        private_file(dump, b"PGDMP" + b"\0" * 1024)
        private_file(
            metadata,
            b'{"schema_version":1,"backup":"release.dump"}\n',
        )
        private_file(
            checksum,
            b"7f83b1657ff1fc53b92dc18148a1d65dfa13514a"
            b"c2d4d6e2c1f47e1b9e1f7a1a2  release.dump\n",
        )
        private_file(secret, secret_payload)
        backup_media, scanned = MEDIA._walk_backup_root(
            self.backup_a,
            self.policy,
            root_authority=MEDIA.capture_backup_root_identity(
                self.backup_a
            ),
        )
        combined = MEDIA.Discovery(
            media={
                **self.discovery.media,
                **{value.media_id: value for value in backup_media},
            },
            docker_inventory_sha256=(
                self.discovery.docker_inventory_sha256
            ),
            backup_inventory_sha256=MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(scanned)
            ),
            scanned_volume_names=self.discovery.scanned_volume_names,
            scanned_bind_sources=self.discovery.scanned_bind_sources,
            scanned_container_ids=self.discovery.scanned_container_ids,
        )

        (registry, _observed), _discover = self._generate(
            [combined, combined]
        )
        by_id = {
            descriptor.media_id: descriptor
            for descriptor in registry.descriptors
        }
        self.assertEqual(
            by_id[f"postgres-backup:{dump}"].audit_method,
            "isolated-backup-restore-read-only",
        )
        for path in (metadata, checksum, secret):
            descriptor_value = by_id[f"reviewed-file:{path}"]
            self.assertEqual(
                descriptor_value.audit_method,
                "reviewed-content-only",
            )
        reviewed_path = self.reviewed / (
            registry.reviewed_content_inventory_sha256.removeprefix(
                "sha256:"
            )
            + ".json"
        )
        reviewed_payload = reviewed_path.read_bytes()
        self.assertNotIn(secret_payload, reviewed_payload)
        self.assertNotIn(secret_payload, registry.payload)
        reviewed_document = json.loads(reviewed_payload)
        secret_record = next(
            record
            for record in reviewed_document["media"]
            if record["media_id"] == f"reviewed-file:{secret}"
        )
        self.assertEqual(secret_record["file_count"], 1)
        self.assertEqual(
            secret_record["source_content_sha256"],
            MEDIA.sha256_bytes(secret_payload),
        )
        self.assertNotIn("payload", secret_record)

    def test_reviewed_file_size_limit_and_between_pass_drift_fail_closed(
        self,
    ) -> None:
        path = self.backup_a / "ordinary.sidecar"
        private_file(path, b"first-secret-value\n")
        source = MEDIA.DiscoveredMedia(
            media_id=f"reviewed-file:{path}",
            kind="reviewed_file",
            locator=str(path),
            data_subpath=".",
            attached=(),
            signature="non-postgres",
            postgres_major=None,
        )
        registry = MEDIA.Registry(
            payload=b"fixture",
            digest="sha256:" + "1" * 64,
            audit_image=MEDIA.POSTGRES_AUDIT_IMAGES[16],
            auditor_sha256=MEDIA._auditor_digest(),
            descriptors=(),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(self.policy),
            audit_images=tuple(
                sorted(MEDIA.POSTGRES_AUDIT_IMAGES.items())
            ),
        )
        original = MEDIA._reviewed_regular_identity
        calls = 0

        def mutate_between_passes(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                private_file(path, b"second-secret-value\n")
            return result

        with (
            mock.patch.object(
                MEDIA,
                "_reviewed_regular_identity",
                side_effect=mutate_between_passes,
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "changed between content CAS passes",
            ),
        ):
            MEDIA._review_non_postgres_file(
                registry,
                source,
                policy=self.policy,
            )

        with path.open("wb") as stream:
            stream.truncate(
                int(
                    MEDIA.LOGICAL_MEDIA_POLICY["non_postgres"][
                        "maximum_single_file_bytes"
                    ]
                )
                + 1
            )
        os.chmod(path, 0o600)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "size limit",
        ):
            MEDIA._review_non_postgres_file(
                registry,
                source,
                policy=self.policy,
            )

    def test_dynamic_classification_seals_pg18_and_non_pg_without_guesses(
        self,
    ) -> None:
        (registry, observed), discover = self._generate(
            [self.discovery, self.discovery]
        )
        self.assertEqual(observed.media, self.discovery.media)
        self.assertEqual(
            observed.docker_inventory_sha256,
            self.discovery.docker_inventory_sha256,
        )
        self.assertEqual(
            set(observed.audit_checkpoints),
            {
                "docker-volume:nexpoly_dev_nexpoly_dev_postgres_data",
                "docker-volume:nexpoly_md_health_opt_app_postgres_data",
                "docker-volume:research_pg18",
            },
        )
        self.assertEqual(discover.call_count, 1)
        pg18 = next(
            value
            for value in registry.descriptors
            if value.media_id == "docker-volume:research_pg18"
        )
        self.assertEqual(pg18.source_postgres_major, 18)
        self.assertEqual(pg18.databases[0]["oid"], "18042")
        self.assertEqual(pg18.databases[0]["owner"], "pg18_owner")
        self.assertIn(
            ("docker-volume:research_pg18", 18, "postgres"),
            self.derive_calls,
        )
        non_pg = next(
            value
            for value in registry.descriptors
            if value.media_id == "docker-volume:model-cache"
        )
        self.assertEqual(non_pg.classification, "reviewed-non-pg")
        self.assertEqual(non_pg.audit_method, "reviewed-content-only")
        self.assertIsNotNone(
            registry.reviewed_content_inventory_sha256
        )

    def test_pg18_descriptor_is_derived_from_full_isolated_audit(self) -> None:
        source = self._source("research_pg18", major=18)
        audited_database = {
            "databases": [
                {
                    **self._database_record(
                        "postgres",
                        oid="18099",
                        owner="observed_owner",
                        audit_role="postgres",
                    ),
                    "audit_state": "complete",
                    "audit": {"ledger": [], "legacy_relation_present": False},
                }
            ]
        }
        with (
            mock.patch.object(
                MEDIA,
                "_isolated_volume_audit",
                return_value=(
                    audited_database,
                    "sha256:" + "1" * 64,
                    {},
                    {},
                    {},
                ),
            ) as isolated,
            mock.patch.object(
                MEDIA,
                "_retained_source_admin_role",
                return_value="postgres",
            ),
        ):
            result = MEDIA._derive_isolated_volume_descriptor(
                self.authority,
                source,
                primary_database="postgres",
                runner=mock.Mock(),
                operation=self.operation,
            )
        provisional = isolated.call_args.args[1]
        self.assertEqual(
            dict(provisional.audit_images)[18],
            MEDIA.POSTGRES_AUDIT_IMAGES[18],
        )
        self.assertTrue(
            isolated.call_args.kwargs["derive_inventory"]
        )
        self.assertEqual(result.source_postgres_major, 18)
        self.assertEqual(result.databases[0]["oid"], "18099")
        self.assertEqual(
            result.databases[0]["owner"],
            "observed_owner",
        )

    def test_every_fixed_root_backup_gets_a_full_pg16_restore_audit(
        self,
    ) -> None:
        path = self.backup_a / "historical.dump"
        source = MEDIA.DiscoveredMedia(
            media_id=f"postgres-backup:{path}",
            kind="postgres_backup",
            locator=str(path),
            data_subpath=".",
            attached=(),
            backup_format="postgres-custom-v1",
            signature="postgres-backup",
            postgres_major=None,
        )
        audited_database = {
            "databases": [
                {
                    **self._database_record(
                        "nexpoly",
                        oid="16444",
                        owner="restored_owner",
                        audit_role="postgres",
                    ),
                    "audit_state": "complete",
                    "audit": {"ledger": [], "legacy_relation_present": False},
                }
            ]
        }
        with mock.patch.object(
            MEDIA,
            "_isolated_backup_audit",
            return_value=(
                audited_database,
                "sha256:" + "1" * 64,
                {},
                {},
                {},
            ),
        ) as isolated:
            result = MEDIA._derive_isolated_backup_descriptor(
                self.authority,
                source,
                runner=mock.Mock(),
                operation=self.operation,
            )
        provisional = isolated.call_args.args[1]
        self.assertEqual(
            provisional.audit_image,
            MEDIA.POSTGRES_AUDIT_IMAGES[16],
        )
        self.assertTrue(
            isolated.call_args.kwargs["derive_inventory"]
        )
        self.assertEqual(result.database, "nexpoly")
        self.assertEqual(result.databases[0]["oid"], "16444")

    def test_second_discovery_cas_preserves_previous_registry_on_drift(
        self,
    ) -> None:
        previous = b"previous-runtime-registry\n"
        private_file(self.registry_path, previous)
        drifted = self._discovery(
            docker_digest="sha256:" + "5" * 64
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "changed before publishing registry",
        ):
            self._generate([self.discovery, drifted])
        self.assertEqual(self.registry_path.read_bytes(), previous)
        self.assertEqual(
            list(self.config.glob(".postgres-media-registry.json.tmp-*")),
            [],
        )

    def test_non_pg_review_is_double_cas_and_postgres_names_stay_blocked(
        self,
    ) -> None:
        source = self._source(
            "model-cache",
            signature="non-postgres",
            major=None,
        )
        identity = {"Name": source.locator, "Driver": "local"}
        with (
            mock.patch.object(
                MEDIA,
                "_docker_volume_identity",
                side_effect=[identity, dict(identity)],
            ),
            mock.patch.object(
                MEDIA,
                "_non_postgres_volume_screen",
                return_value={"file_count": 2, "size_bytes": 16},
            ),
            mock.patch.object(
                MEDIA,
                "_volume_content_digest",
                return_value="sha256:" + "6" * 64,
            ),
        ):
            record = MEDIA._review_non_postgres_volume(
                MEDIA.Registry(
                    payload=b"fixture",
                    digest="sha256:" + "1" * 64,
                    audit_image=MEDIA.POSTGRES_AUDIT_IMAGES[16],
                    auditor_sha256=MEDIA._auditor_digest(),
                    descriptors=(),
                    required_online_databases=(),
                    boundary={},
                    audit_images=tuple(
                        sorted(MEDIA.POSTGRES_AUDIT_IMAGES.items())
                    ),
                ),
                source,
                runner=mock.Mock(),
                operation=self.operation,
                resource_prefix="non-pg",
            )
        self.assertEqual(record["file_count"], 2)
        self.assertEqual(
            record["source_content_sha256"],
            "sha256:" + "6" * 64,
        )
        with (
            mock.patch.object(
                MEDIA,
                "_docker_volume_identity",
                side_effect=[identity, dict(identity)],
            ),
            mock.patch.object(
                MEDIA,
                "_non_postgres_volume_screen",
                return_value={"file_count": 2, "size_bytes": 16},
            ),
            mock.patch.object(
                MEDIA,
                "_volume_content_digest",
                side_effect=[
                    "sha256:" + "6" * 64,
                    "sha256:" + "7" * 64,
                ],
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "changed during reviewed-content audit",
            ),
        ):
            MEDIA._review_non_postgres_volume(
                MEDIA.Registry(
                    payload=b"fixture",
                    digest="sha256:" + "1" * 64,
                    audit_image=MEDIA.POSTGRES_AUDIT_IMAGES[16],
                    auditor_sha256=MEDIA._auditor_digest(),
                    descriptors=(),
                    required_online_databases=(),
                    boundary={},
                    audit_images=tuple(
                        sorted(MEDIA.POSTGRES_AUDIT_IMAGES.items())
                    ),
                ),
                source,
                runner=mock.Mock(),
                operation=self.operation,
                resource_prefix="non-pg-drift",
            )

        named = self._source(
            "looks-like-postgres-cache",
            signature="non-postgres",
            major=None,
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "non-Postgres-named",
        ):
            MEDIA._non_postgres_volume_screen(
                mock.Mock(),
                MEDIA.POSTGRES_AUDIT_IMAGES[16],
                named,
                operation=self.operation,
                resource_key="screen",
            )

    def test_atomic_publish_failure_keeps_previous_generation(self) -> None:
        target = self.config / "generated.json"
        private_file(target, b"previous\n")

        def reject(_path: Path) -> None:
            raise MEDIA.MediaEvidenceError("injected staged validation crash")

        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "injected staged validation crash",
        ):
            MEDIA._replace_private_atomic(
                self.config,
                target.name,
                b"next\n",
                validate_staged=reject,
            )
        self.assertEqual(target.read_bytes(), b"previous\n")
        self.assertEqual(
            list(self.config.glob(".generated.json.tmp-*")),
            [],
        )

    def test_generation_resumes_durable_offline_checkpoints(self) -> None:
        self._generate([self.discovery, self.discovery])
        initial_calls = list(self.derive_calls)
        self.derive_calls.clear()
        with mock.patch.object(
            MEDIA,
            "_revalidate_durable_checkpoint_source",
        ) as content_cas:
            self._generate([self.discovery, self.discovery])
        self.assertTrue(initial_calls)
        self.assertEqual(self.derive_calls, [])
        self.assertEqual(
            content_cas.call_count,
            len(initial_calls),
        )

    def test_cli_has_no_scan_root_registry_or_digest_override(self) -> None:
        for arguments in (
            ["build", "--registry", "/tmp/foreign.json"],
            ["build", "--backup-root", "/tmp/foreign"],
            ["build", "--image", "postgres:latest"],
            ["build", "--authority-digest", "sha256:" + "1" * 64],
        ):
            with (
                self.subTest(arguments=arguments),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                MEDIA.parser().parse_args(arguments)


class DurableCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="postgres-media-checkpoint-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.evidence = self.root / "evidence"
        private_directory(self.evidence)
        self.policy = MEDIA.DiscoveryPolicy(backup_roots=())
        self.boundary = MEDIA.seal_discovery_boundary(self.policy)
        self.image_ids = tuple(
            (
                major,
                "sha256:" + f"{major:02x}" * 32,
            )
            for major in sorted(MEDIA.POSTGRES_AUDIT_IMAGES)
        )
        self.source = MEDIA.DiscoveredMedia(
            media_id="docker-volume:dormant-checkpoint",
            kind="docker_volume",
            locator="dormant-checkpoint",
            data_subpath=".",
            attached=(),
            signature="postgres",
            postgres_major=16,
            docker_inspect_sha256="sha256:" + "6" * 64,
        )
        self.database_record = {
            "name": "postgres",
            "oid": "16420",
            "owner": "postgres",
            "allow_connections": True,
            "template": False,
            "audit_role": "postgres",
            "migration_scope": "nexpoly-ledger",
        }
        self.descriptor = MEDIA.MediaDescriptor(
            media_id=self.source.media_id,
            kind=self.source.kind,
            database="postgres",
            database_user="postgres",
            disposition="retained-private-isolated",
            audit_method="isolated-volume-copy-read-only",
            classification="nexpoly-db",
            source_postgres_major=16,
            databases=(dict(self.database_record),),
        )
        self.production_identity = {
            "stack": "production",
            "database": "nexpoly",
            "kind": "docker_volume",
            "media_id": self.source.media_id,
            "postgres_major": 16,
            "system_identifier": "7312345678901234567",
        }
        self.registry = MEDIA.Registry(
            payload=b"fixture",
            digest="sha256:" + "1" * 64,
            audit_image=MEDIA.POSTGRES_AUDIT_IMAGES[16],
            auditor_sha256=MEDIA._auditor_digest(),
            descriptors=(self.descriptor,),
            required_online_databases=(),
            boundary=self.boundary,
            authority_rules_sha256="sha256:" + "2" * 64,
            audit_images=tuple(
                sorted(MEDIA.POSTGRES_AUDIT_IMAGES.items())
            ),
            audit_image_ids=self.image_ids,
            postgres_uid=MEDIA.POSTGRES_UID,
            postgres_gid=MEDIA.POSTGRES_GID,
            production_identity=dict(self.production_identity),
        )
        self.authority = MEDIA.MediaAuthorityRules(
            payload=b"fixture-authority",
            digest=self.registry.authority_rules_sha256,
            audit_image=self.registry.audit_image,
            auditor_sha256=self.registry.auditor_sha256,
            descriptors=(),
            required_online_databases=(),
            policy=self.policy,
            allow_unmatched_non_postgres=False,
            production_identity=dict(self.production_identity),
            audit_images=self.registry.audit_images,
            logical_media=copy.deepcopy(MEDIA.LOGICAL_MEDIA_POLICY),
        )
        self.source_identity = {
            "name": self.source.locator,
            "driver": "local",
            "mountpoint": (
                "/var/lib/docker/volumes/"
                f"{self.source.locator}/_data"
            ),
            "labels_sha256": "sha256:" + "7" * 64,
            "inspect_sha256": "sha256:" + "6" * 64,
            "data_subpath": ".",
            "attached": [],
        }
        database = {
            "databases": [
                {
                    **self.database_record,
                    "audit_state": "complete",
                    "audit": {},
                }
            ]
        }
        self.core_checkpoint = {
            "schema_version": 1,
            "media_id": self.source.media_id,
            "source_document_sha256": MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(self.source.document())
            ),
            "descriptor": self.descriptor.document(),
            "descriptor_sha256": MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(
                    self.descriptor.document()
                )
            ),
            "method": self.descriptor.audit_method,
            "database": database,
            "source_content_sha256": "sha256:" + "8" * 64,
            "source_identity_before": dict(self.source_identity),
            "source_identity_after": dict(self.source_identity),
            "isolation": {
                "source_mounted_read_only": True,
                "source_started_as_postgres": False,
                "scratch_network": "none",
                "scratch_destroyed": True,
                "copy_method": (
                    "readonly-tar-copy-to-disposable-volume-v1"
                ),
            },
            "scope": "copied-source-cluster",
            "algorithm": "postgres-data-directory-tar-sha256-v1",
        }
        MEDIA._validate_durable_checkpoint_directory(self.evidence)
        self.checkpoint = MEDIA._publish_durable_checkpoint(
            self.evidence,
            self.registry,
            self.source,
            self.core_checkpoint,
        )

    def operation(self):
        operation = mock.Mock()
        operation.workspace = self.root
        operation.authority = {
            "postgres_images": {
                str(major): {
                    "digest_ref": dict(
                        self.registry.audit_images
                    )[major],
                    "image_id": image_id,
                }
                for major, image_id in self.registry.audit_image_ids
            }
        }
        return operation

    def test_checkpoint_is_private_durable_and_authority_bound(self) -> None:
        path = MEDIA._durable_checkpoint_path(
            self.evidence,
            self.source.media_id,
        )
        metadata = path.lstat()
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
        self.assertEqual(metadata.st_nlink, 1)
        resumed = MEDIA._load_durable_checkpoint(
            self.evidence,
            registry=self.registry,
            source=self.source,
            expected_descriptor=self.descriptor,
            allow_descriptor_inventory=False,
            runner=mock.Mock(),
            operation=self.operation(),
            policy=self.policy,
            revalidate_source=False,
        )
        self.assertIsNotNone(resumed)
        assert resumed is not None
        self.assertEqual(resumed[0], self.descriptor)
        self.assertEqual(resumed[1], self.checkpoint)

        changed_registry = replace(
            self.registry,
            auditor_sha256="sha256:" + "9" * 64,
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "authority or source differs",
        ):
            MEDIA._load_durable_checkpoint(
                self.evidence,
                registry=changed_registry,
                source=self.source,
                expected_descriptor=self.descriptor,
                allow_descriptor_inventory=False,
                runner=mock.Mock(),
                operation=self.operation(),
                policy=self.policy,
                revalidate_source=False,
            )

    def test_checkpoint_reuse_requires_exact_source_content_cas(self) -> None:
        with (
            mock.patch.object(
                MEDIA,
                "_docker_volume_identity",
                side_effect=[
                    dict(self.source_identity),
                    dict(self.source_identity),
                ],
            ),
            mock.patch.object(
                MEDIA,
                "_volume_content_digest",
                return_value="sha256:" + "a" * 64,
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "source content differs",
            ),
        ):
            MEDIA._load_durable_checkpoint(
                self.evidence,
                registry=self.registry,
                source=self.source,
                expected_descriptor=self.descriptor,
                allow_descriptor_inventory=False,
                runner=mock.Mock(),
                operation=self.operation(),
                policy=self.policy,
                revalidate_source=True,
            )

    def test_lightweight_registry_load_attaches_checkpoint_without_restore(
        self,
    ) -> None:
        discovery = MEDIA.Discovery(
            media={self.source.media_id: self.source},
            docker_inventory_sha256="sha256:" + "b" * 64,
            backup_inventory_sha256="sha256:" + "c" * 64,
            scanned_volume_names=(self.source.locator,),
            scanned_bind_sources=(),
            scanned_container_ids=(),
        )
        with (
            mock.patch.object(
                MEDIA,
                "load_registry",
                return_value=self.registry,
            ),
            mock.patch.object(
                MEDIA,
                "discover_media",
                return_value=discovery,
            ),
            mock.patch.object(MEDIA, "_revalidate_docker_epoch"),
            mock.patch.object(MEDIA, "_revalidate_backup_epoch"),
            mock.patch.object(
                MEDIA,
                "_revalidate_live_registry_epoch",
            ),
            mock.patch.object(
                MEDIA,
                "_isolated_volume_audit",
            ) as isolated,
        ):
            registry, observed = (
                MEDIA.load_runtime_registry_for_revalidation(
                    self.authority,
                    registry_path=self.root / "registry.json",
                    evidence_root=self.evidence,
                    runner=mock.Mock(),
                    operation=self.operation(),
                )
            )
        self.assertEqual(registry, self.registry)
        self.assertEqual(
            observed.audit_checkpoints,
            {self.source.media_id: self.checkpoint},
        )
        isolated.assert_not_called()

    def test_build_consumes_checkpoint_without_copy_start_or_restore(
        self,
    ) -> None:
        audit = database_audit(
            "postgres",
            "postgres",
            system_identifier="7312345678901234567",
            database_oid="16420",
        )
        audit.pop("_unused_empty_digest")
        inventory = [
            {
                key: self.database_record[key]
                for key in (
                    "name",
                    "oid",
                    "owner",
                    "allow_connections",
                    "template",
                )
            }
        ]
        database = {
            **audit,
            "database_inventory": inventory,
            "database_inventory_sha256": MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(inventory)
            ),
            "databases": [
                {
                    **self.database_record,
                    "audit_state": "complete",
                    "audit": audit,
                }
            ],
        }
        checkpoint = MEDIA._publish_durable_checkpoint(
            self.evidence,
            self.registry,
            self.source,
            {
                **self.core_checkpoint,
                "database": database,
            },
        )
        registry = replace(
            self.registry,
            production_identity=None,
        )
        discovery = MEDIA.Discovery(
            media={self.source.media_id: self.source},
            docker_inventory_sha256="sha256:" + "b" * 64,
            backup_inventory_sha256="sha256:" + "c" * 64,
            scanned_volume_names=(self.source.locator,),
            scanned_bind_sources=(),
            scanned_container_ids=(),
            audit_checkpoints={
                self.source.media_id: checkpoint,
            },
        )
        image_id = dict(registry.audit_image_ids)[16]
        with (
            mock.patch.object(
                MEDIA,
                "_validate_audit_image",
                return_value=image_id,
            ),
            mock.patch.object(
                MEDIA,
                "_docker_volume_identity",
                side_effect=[
                    dict(self.source_identity),
                    dict(self.source_identity),
                ],
            ),
            mock.patch.object(
                MEDIA,
                "_volume_content_digest",
                return_value=self.core_checkpoint[
                    "source_content_sha256"
                ],
            ),
            mock.patch.object(MEDIA, "_revalidate_docker_epoch"),
            mock.patch.object(MEDIA, "_revalidate_backup_epoch"),
            mock.patch.object(
                MEDIA,
                "_isolated_volume_audit",
            ) as volume_audit,
            mock.patch.object(
                MEDIA,
                "_isolated_bind_audit",
            ) as bind_audit,
            mock.patch.object(
                MEDIA,
                "_isolated_backup_audit",
            ) as backup_audit,
        ):
            envelope = MEDIA.build_evidence(
                registry,
                discovery,
                runner=mock.Mock(),
                evidence_root=self.evidence,
                operation=self.operation(),
                now=lambda: "2026-07-18T12:00:00Z",
            )
        self.assertEqual(envelope["schema_version"], 5)
        self.assertEqual(
            envelope["media"][0]["audit"]["method"],
            "isolated-volume-copy-read-only",
        )
        volume_audit.assert_not_called()
        bind_audit.assert_not_called()
        backup_audit.assert_not_called()


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
                "statement_timeout": "5min",
                "lock_timeout": "5s",
                "search_path": "pg_catalog",
                "row_security": True,
                **database_startup_fields(
                    "/var/lib/postgresql/data",
                    isolated=True,
                ),
                "role_superuser": True,
                "role_create_db": True,
                "role_create_role": True,
                "role_replication": True,
                "role_bypass_rls": True,
                "role_inherit": True,
                "role_can_login": True,
                **role_security_fields("nexpoly", superuser=True),
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
                "generation_schema": None,
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
            "persistence": "p",
            "is_partition": False,
            "row_security": False,
            "force_row_security": False,
            "reloptions": [],
            "replica_identity": "d",
            "tablespace_oid": "0",
            "access_method": "heap",
            "partition_bound": None,
            "acl": [],
            "column_acl": [],
            "comments": [],
            "initial_privileges": [],
            "subscriptions": [],
            "policies": [],
            "publications": [],
            "extended_statistics": [],
            "security_labels": [],
            "parents": [],
            "children": [],
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
            "triggers": [],
            "rewrite_rules": [],
            "referencing_foreign_keys": [],
            "unapproved_drop_dependents": [],
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

    def test_isolated_startup_command_sentinels_are_exact(self) -> None:
        baseline = [
            json.loads(line)
            for line in self.payload(
                self.ledger_relation()
            ).decode("utf-8").splitlines()
        ]
        for field, value in (
            ("archive_command", ""),
            ("archive_command", "/bin/false"),
            ("restore_command", ""),
            ("restore_command", "(disabled)"),
        ):
            with self.subTest(field=field, value=value):
                records = copy.deepcopy(baseline)
                records[0][field] = value
                payload = b"\n".join(
                    json.dumps(record, sort_keys=True).encode("utf-8")
                    for record in records
                ) + b"\n"
                with self.assertRaisesRegex(
                    MEDIA.MediaEvidenceError,
                    "startup configuration is unsafe",
                ):
                    MEDIA._parse_database_audit(
                        payload,
                        expected_database="nexpoly",
                        expected_user="postgres",
                        isolated=True,
                    )

    def test_descriptor_binds_execution_mode_and_online_client_id(
        self,
    ) -> None:
        authority = {
            "name": "nexpoly",
            "oid": "16384",
            "owner": "postgres",
            "allow_connections": True,
            "template": False,
            "audit_role": "postgres",
            "migration_scope": "nexpoly-ledger",
        }
        descriptor = MEDIA.MediaDescriptor(
            media_id="docker-volume:fixture",
            kind="docker_volume",
            database="nexpoly",
            database_user="postgres",
            disposition="retained-private-isolated",
            audit_method="isolated-volume-copy-read-only",
            online_admin_role="postgres",
            source_postgres_major=16,
            databases=(authority,),
        )
        runner = mock.Mock(spec=MEDIA.CommandRunner)
        runner.run.return_value = subprocess.CompletedProcess(
            ["docker", "exec"],
            0,
            stdout=self.payload(self.ledger_relation()),
            stderr=b"",
        )
        result = MEDIA._audit_container_database(
            runner,
            CONTAINER_A,
            descriptor,
            database_authority=authority,
            isolated=True,
        )
        self.assertEqual(
            result["database_identity"]["database"],
            "nexpoly",
        )
        runner.run.assert_called_once()

        online_descriptor = replace(
            descriptor,
            disposition="read-only-online",
            audit_method="live-read-only",
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "online database audit lacks its exact client image ID",
        ):
            MEDIA._audit_container_database(
                mock.Mock(spec=MEDIA.CommandRunner),
                CONTAINER_A,
                online_descriptor,
                database_authority=authority,
                isolated=False,
            )
        for current, isolated in (
            (online_descriptor, True),
            (descriptor, False),
        ):
            with self.subTest(
                audit_method=current.audit_method,
                isolated=isolated,
            ):
                mismatch_runner = mock.Mock(
                    spec=MEDIA.CommandRunner
                )
                with self.assertRaisesRegex(
                    MEDIA.MediaEvidenceError,
                    "execution mode differs from descriptor authority",
                ):
                    MEDIA._audit_container_database(
                        mismatch_runner,
                        CONTAINER_A,
                        current,
                        database_authority=authority,
                        isolated=isolated,
                        trusted_image_id=IMAGE_ID,
                    )
                mismatch_runner.run.assert_not_called()

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
            (
                "row-security",
                lambda relation: relation.update({"row_security": True}),
            ),
            (
                "dormant-policy",
                lambda relation: relation.update(
                    {"policies": ["disabled_but_unapproved"]}
                ),
            ),
            (
                "publication",
                lambda relation: relation.update(
                    {"publications": ["table:unapproved_publication"]}
                ),
            ),
            (
                "extended-statistics",
                lambda relation: relation.update(
                    {"extended_statistics": ["governance.unapproved_stats"]}
                ),
            ),
            (
                "security-label",
                lambda relation: relation.update(
                    {
                        "security_labels": [
                            {
                                "provider": "unapproved",
                                "label": "external",
                                "subobject": 0,
                            }
                        ]
                    }
                ),
            ),
            (
                "force-row-security",
                lambda relation: relation.update(
                    {"force_row_security": True}
                ),
            ),
            (
                "unlogged",
                lambda relation: relation.update({"persistence": "u"}),
            ),
            (
                "partition",
                lambda relation: relation.update({"is_partition": True}),
            ),
            (
                "reloptions",
                lambda relation: relation.update(
                    {"reloptions": ["autovacuum_enabled=false"]}
                ),
            ),
            (
                "user-trigger",
                lambda relation: relation.update(
                    {
                        "triggers": [
                            {
                                "name": "evil_after_insert",
                                "enabled": "O",
                                "definition": (
                                    "CREATE TRIGGER evil_after_insert "
                                    "AFTER INSERT ON governance.schema_migrations "
                                    "FOR EACH ROW EXECUTE FUNCTION public.evil()"
                                ),
                            }
                        ]
                    }
                ),
            ),
            (
                "rewrite-rule",
                lambda relation: relation.update(
                    {
                        "rewrite_rules": [
                            {
                                "name": "evil_rule",
                                "event": "2",
                                "enabled": "O",
                                "instead": False,
                                "definition": (
                                    "CREATE RULE evil_rule AS ON INSERT TO "
                                    "governance.schema_migrations DO NOTHING"
                                ),
                            }
                        ]
                    }
                ),
            ),
            (
                "inheritance-parent",
                lambda relation: relation.update(
                    {"parents": ["public.attacker_parent"]}
                ),
            ),
            (
                "inheritance-child",
                lambda relation: relation.update(
                    {"children": ["public.attacker_child"]}
                ),
            ),
            (
                "inbound-foreign-key",
                lambda relation: relation.update(
                    {
                        "referencing_foreign_keys": [
                            {
                                "relation": "public.attacker",
                                "name": "attacker_fk",
                                "definition": (
                                    "FOREIGN KEY (job_id) REFERENCES "
                                    "generation.polytao_jobs(job_id)"
                                ),
                            }
                        ]
                    }
                ),
            ),
            (
                "external-dependent-view",
                lambda relation: relation.update(
                    {
                        "unapproved_drop_dependents": [
                            {
                                "class": "pg_rewrite",
                                "object": (
                                    "rule _RETURN on view "
                                    "public.polytao_jobs_projection"
                                ),
                                "referenced_subobject": 0,
                                "dependency_type": "n",
                            }
                        ]
                    }
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

    def test_database_event_trigger_is_rejected(self) -> None:
        records = [
            json.loads(line)
            for line in self.payload(
                self.ledger_relation()
            ).decode("utf-8").splitlines()
        ]
        records[0]["event_triggers"] = [
            {
                "name": "unapproved_ddl",
                "event": "ddl_command_start",
                "enabled": "O",
                "function": "public.unapproved_ddl()",
                "tags": [],
            }
        ]
        payload = b"\n".join(
            json.dumps(record, sort_keys=True).encode("utf-8")
            for record in records
        ) + b"\n"
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "identity|read-only",
        ):
            MEDIA._parse_database_audit(
                payload,
                expected_database="nexpoly",
                expected_user="postgres",
                isolated=True,
            )

    def test_online_audit_role_rejects_every_privilege_and_membership(
        self,
    ) -> None:
        baseline = database_sql_payload(
            database="nexpoly",
            user="nexpoly_auditor",
            oid="16384",
            owner="postgres",
        )
        records = [
            json.loads(line)
            for line in baseline.decode("utf-8").splitlines()
        ]
        for field, value in (
            ("role_superuser", True),
            ("role_create_db", True),
            ("role_create_role", True),
            ("role_replication", True),
            ("role_bypass_rls", True),
            ("role_inherit", True),
            ("role_can_login", True),
            ("role_memberships", ["dangerous_parent"]),
        ):
            tampered = copy.deepcopy(records)
            tampered[0][field] = value
            payload = b"\n".join(
                json.dumps(record, sort_keys=True).encode("utf-8")
                for record in tampered
            ) + b"\n"
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    MEDIA.MediaEvidenceError,
                    "role|privileged",
                ):
                    MEDIA._parse_database_audit(
                        payload,
                        expected_database="nexpoly",
                        expected_user="nexpoly_auditor",
                        isolated=False,
                    )
        template = (
            ROOT / "ops/config/postgres-media-audit-role.sql.example"
        ).read_text(encoding="utf-8")
        for token in (
            "NOREPLICATION",
            "NOBYPASSRLS",
            "NOINHERIT",
            "NOLOGIN",
            "pg_auth_members",
            "role_contract_sha256",
            "NEXPOLY_PROVISION_REFUSED_ROLE_COLLISION",
            "shobj_description",
        ):
            self.assertIn(token, template)
        for forbidden in (
            'ALTER ROLE :"audit_role"\n  NOSUPERUSER',
            "REVOKE ALL PRIVILEGES",
            "RESET ALL",
            "REASSIGN OWNED",
            "DROP OWNED",
        ):
            self.assertNotIn(forbidden, template)

    def test_online_audit_role_requires_the_planned_contract_marker(
        self,
    ) -> None:
        expected = "sha256:" + "a" * 64
        baseline = database_sql_payload(
            database="nexpoly",
            user="nexpoly_auditor",
            oid="16384",
            owner="postgres",
        )
        parsed = MEDIA._parse_database_audit(
            baseline,
            expected_database="nexpoly",
            expected_user="nexpoly_auditor",
            isolated=False,
            expected_role_contract_sha256=expected,
        )
        self.assertEqual(parsed["role_contract_sha256"], expected)

        records = [
            json.loads(line)
            for line in baseline.decode("utf-8").splitlines()
        ]
        records[0]["role_contract_marker"] = (
            MEDIA.ROLE_CONTRACT_POLICY + ":sha256:" + "b" * 64
        )
        payload = b"\n".join(
            json.dumps(record, sort_keys=True).encode("utf-8")
            for record in records
        ) + b"\n"
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "contract marker differs",
        ):
            MEDIA._parse_database_audit(
                payload,
                expected_database="nexpoly",
                expected_user="nexpoly_auditor",
                isolated=False,
                expected_role_contract_sha256=expected,
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
    if not role_superuser and user != owner:
        relation["acl"] = [
            {
                "grantee": user,
                "privilege": "SELECT",
                "grantable": False,
            }
        ]
    records = [
        {
            "record_type": "database",
            "database": database,
            "current_user": user,
            "transaction_read_only": True,
            "statement_timeout": "5min",
            "lock_timeout": "5s",
            "search_path": "pg_catalog",
            "row_security": True,
            **database_startup_fields(data_directory),
            "role_superuser": role_superuser,
            "role_create_db": role_superuser,
            "role_create_role": role_superuser,
            "role_replication": role_superuser,
            "role_bypass_rls": role_superuser,
            "role_inherit": role_superuser,
            "role_can_login": role_superuser,
            "role_contract_marker": (
                None
                if role_superuser
                else (
                    MEDIA.ROLE_CONTRACT_POLICY
                    + ":sha256:"
                    + "a" * 64
                )
            ),
            **role_security_fields(
                database,
                superuser=role_superuser,
            ),
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
            "generation_schema": None,
            "relation": None,
            "rows": [],
        },
    ]
    return b"\n".join(
        json.dumps(value, sort_keys=True).encode("utf-8")
        for value in records
    ) + b"\n"


class AdjacentPostgresRoleAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = MEDIA.DiscoveredMedia(
            media_id="docker-volume:research-adjacent",
            kind="docker_volume",
            locator="research-adjacent",
            data_subpath=".",
            attached=(attachment_record(CONTAINER_A),),
            signature="postgres",
            postgres_major=16,
        )
        self.authority = MEDIA.MediaAuthorityRules(
            payload=b"fixture",
            digest="sha256:" + "a" * 64,
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            descriptors=(),
            required_online_databases=(),
            policy=MEDIA.DiscoveryPolicy(),
            allow_unmatched_non_postgres=False,
            production_identity={},
            audit_images=tuple(
                sorted(MEDIA.POSTGRES_AUDIT_IMAGES.items())
            ),
        )
        self.inventory = [
            {
                "name": "Research Data",
                "oid": "17000",
                "owner": "Poly Admin",
                "allow_connections": True,
                "template": False,
            },
            {
                "name": "postgres",
                "oid": "5",
                "owner": "Poly Admin",
                "allow_connections": True,
                "template": False,
            },
        ]

    def descriptor(self) -> MEDIA.MediaDescriptor:
        with (
            mock.patch.object(
                MEDIA,
                "_online_container_admin_role",
                return_value="Poly Admin",
            ),
            mock.patch.object(
                MEDIA,
                "_container_database_inventory",
                return_value=copy.deepcopy(self.inventory),
            ),
        ):
            return MEDIA._live_adjacent_runtime_descriptor(
                self.authority,
                self.source,
                runner=mock.Mock(),
                audit_image_id=IMAGE_ID,
            )

    def test_adjacent_uses_exact_admin_without_cluster_mutation(self) -> None:
        first = self.descriptor()
        second = self.descriptor()
        self.assertEqual(first.document(), second.document())
        self.assertEqual(first.online_admin_role, "Poly Admin")
        self.assertEqual(first.database_user, "Poly Admin")
        roles = [record["audit_role"] for record in first.databases]
        self.assertEqual(roles, ["Poly Admin", "Poly Admin"])

    def test_role_plan_never_mutates_an_adjacent_cluster(self) -> None:
        current = self.descriptor()
        registry = MEDIA.Registry(
            payload=b"registry",
            digest="sha256:" + "b" * 64,
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            descriptors=(current,),
            required_online_databases=(),
            boundary={},
            authority_rules_sha256=self.authority.digest,
        )
        discovery = MEDIA.Discovery(
            media={self.source.media_id: self.source},
            docker_inventory_sha256="sha256:" + "c" * 64,
            backup_inventory_sha256="sha256:" + "d" * 64,
            scanned_volume_names=(self.source.locator,),
            scanned_bind_sources=(),
            scanned_container_ids=(CONTAINER_A,),
        )
        sql = b"SELECT 'pinned role contract';\n"
        sql_digest = MEDIA.sha256_bytes(sql)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "no online databases",
        ):
            MEDIA.external_database_role_plan(
                registry,
                discovery,
                role_sql_sha256=sql_digest,
                runner=mock.Mock(),
            )

    def test_pg14_sql_omits_parameter_acl_catalog(self) -> None:
        self.assertNotIn(
            "pg_parameter_acl",
            MEDIA._database_audit_sql_for_major(14),
        )
        self.assertIn(
            "pg_parameter_acl",
            MEDIA._database_audit_sql_for_major(15),
            )


class ManagedRoleMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.databases = (
            {
                "name": "nexpoly",
                "oid": "16384",
                "owner": "polyprop",
                "allow_connections": True,
                "template": False,
                "audit_role": "nexpoly_auditor",
                "migration_scope": "nexpoly-ledger",
            },
            {
                "name": "postgres",
                "oid": "5",
                "owner": "polyprop",
                "allow_connections": True,
                "template": False,
                "audit_role": "nexpoly_postgres_auditor",
                "migration_scope": "adjacent-record-only",
            },
        )
        self.contracts = {
            "nexpoly_auditor": "sha256:" + "a" * 64,
            "nexpoly_postgres_auditor": "sha256:" + "b" * 64,
        }

    def role(
        self,
        role_name: str,
        *,
        current_database: str,
        marker: str | None = None,
        local_acl: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        target = next(
            value["name"]
            for value in self.databases
            if value["audit_role"] == role_name
        )
        if local_acl is None:
            local_acl = []
            if current_database == target:
                local_acl = [
                    {
                        "object_kind": "function",
                        "object_name": (
                            "pg_catalog.pg_control_system()"
                        ),
                        "privilege": "EXECUTE",
                        "grantable": False,
                    }
                ]
                if current_database == "nexpoly":
                    local_acl.extend(
                        [
                            {
                                "object_kind": "relation",
                                "object_name": (
                                    "governance.schema_migrations"
                                ),
                                "privilege": "SELECT",
                                "grantable": False,
                            },
                            {
                                "object_kind": "schema",
                                "object_name": "governance",
                                "privilege": "USAGE",
                                "grantable": False,
                            },
                        ]
                    )
        return {
            "name": role_name,
            "marker": (
                marker
                if marker is not None
                else (
                    MEDIA.ROLE_CONTRACT_POLICY
                    + ":"
                    + self.contracts[role_name]
                )
            ),
            "superuser": False,
            "create_db": False,
            "create_role": False,
            "replication": False,
            "bypass_rls": False,
            "inherit": False,
            "can_login": False,
            "memberships": [],
            "incoming_memberships": [],
            "settings": [
                "default_transaction_read_only=on",
                "lock_timeout=5s",
                "statement_timeout=5min",
            ],
            "shared_owned_objects": [],
            "shared_direct_acl": [
                {
                    "object_kind": "database",
                    "object_name": target,
                    "privilege": "CONNECT",
                    "grantable": False,
                }
            ],
            "local_owned_objects": [],
            "local_direct_acl": sorted(
                local_acl,
                key=MEDIA.canonical_json_bytes,
            ),
            "local_default_acl": [],
            "local_effective_persistent_write": [],
        }

    def payload(
        self,
        database: dict[str, object],
        *,
        mutate: Callable[[list[dict[str, object]]], None]
        | None = None,
    ) -> bytes:
        roles = [
            self.role(
                str(authority["audit_role"]),
                current_database=str(database["name"]),
            )
            for authority in self.databases
        ]
        if mutate is not None:
            mutate(roles)
        return (
            json.dumps(
                {
                    "record_type": "managed_role_matrix",
                    "database": database["name"],
                    "database_oid": database["oid"],
                    "database_owner": database["owner"],
                    "ledger_present": (
                        database["name"] == "nexpoly"
                    ),
                    "legacy_present": False,
                    "roles": roles,
                },
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )

    def run_matrix(
        self,
        *,
        mutate_by_database: dict[
            str,
            Callable[[list[dict[str, object]]], None],
        ]
        | None = None,
        databases: tuple[dict[str, object], ...] | None = None,
    ) -> tuple[dict[str, object], mock.Mock]:
        selected_databases = databases or self.databases
        mutations = mutate_by_database or {}

        def execute(_runner, **kwargs):
            arguments = kwargs["arguments"]
            database_name = arguments[
                arguments.index("-d") + 1
            ]
            database = next(
                value
                for value in selected_databases
                if value["name"] == database_name
            )
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=self.payload(
                    database,
                    mutate=mutations.get(database_name),
                ),
            )

        trusted_psql = mock.Mock(side_effect=execute)
        with mock.patch.object(
            MEDIA,
            "_run_trusted_psql",
            trusted_psql,
        ):
            result = MEDIA._managed_role_matrix(
                mock.Mock(spec=MEDIA.CommandRunner),
                container_id=CONTAINER_A,
                databases=selected_databases,
                online_admin_role="polyprop",
                postgres_major=16,
                trusted_image_id=IMAGE_ID,
                expected_contracts=self.contracts,
                allow_missing=False,
            )
        return result, trusted_psql

    def test_cartesian_role_database_matrix_accepts_only_exact_grants(
        self,
    ) -> None:
        result, trusted_psql = self.run_matrix()
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(len(result["roles"]), 2)
        self.assertEqual(trusted_psql.call_count, 2)
        for current in trusted_psql.call_args_list:
            arguments = current.kwargs["arguments"]
            self.assertIn(
                "managed_role_0=nexpoly_auditor",
                arguments,
            )
            self.assertIn(
                "managed_role_1=nexpoly_postgres_auditor",
                arguments,
            )

    def test_cross_database_acl_and_orphan_marker_fail_closed(
        self,
    ) -> None:
        def cross_database_acl(
            roles: list[dict[str, object]],
        ) -> None:
            roles[0]["local_direct_acl"] = [
                {
                    "object_kind": "relation",
                    "object_name": "public.foreign_table",
                    "privilege": "SELECT",
                    "grantable": False,
                }
            ]

        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "cross-database ACL",
        ):
            self.run_matrix(
                mutate_by_database={
                    "postgres": cross_database_acl
                }
            )

        def orphan_marker(
            roles: list[dict[str, object]],
        ) -> None:
            orphan = copy.deepcopy(roles[0])
            orphan["name"] = "nexpoly_orphan_auditor"
            orphan["marker"] = (
                MEDIA.ROLE_CONTRACT_POLICY
                + ":sha256:"
                + "c" * 64
            )
            orphan["shared_direct_acl"] = []
            orphan["local_direct_acl"] = []
            roles.append(orphan)

        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "orphan managed audit-role",
        ):
            self.run_matrix(
                mutate_by_database={
                    "nexpoly": orphan_marker,
                    "postgres": orphan_marker,
                }
            )

    def test_unmarked_collision_and_multi_database_role_reuse_are_rejected(
        self,
    ) -> None:
        def unmarked(
            roles: list[dict[str, object]],
        ) -> None:
            roles[0]["marker"] = None

        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "global contract differs",
        ):
            self.run_matrix(
                mutate_by_database={
                    "nexpoly": unmarked,
                    "postgres": unmarked,
                }
            )

        duplicate = tuple(
            copy.deepcopy(value) for value in self.databases
        )
        duplicate[1]["audit_role"] = duplicate[0]["audit_role"]
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "database authority is invalid",
        ):
            self.run_matrix(databases=duplicate)

    def test_sql_is_major_specific_and_has_no_unresolved_fragment(
        self,
    ) -> None:
        pg14 = MEDIA._managed_role_matrix_sql(
            2,
            postgres_major=14,
        )
        pg16 = MEDIA._managed_role_matrix_sql(
            2,
            postgres_major=16,
        )
        self.assertNotIn("pg_parameter_acl", pg14)
        self.assertIn("pg_parameter_acl", pg16)
        self.assertNotIn("__NEXPOLY_", pg14)
        self.assertNotIn("__NEXPOLY_", pg16)


class ExternalRoleProvisioningV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.mount = {
            "Type": "volume",
            "Name": "role-fixture",
            "Source": (
                "/var/lib/docker/volumes/role-fixture/_data"
            ),
            "Destination": "/var/lib/postgresql/data",
            "RW": True,
        }
        self.container = {
            "Id": CONTAINER_A,
            "Name": "/role-fixture",
            "Image": "sha256:" + "4" * 64,
            "Created": "2026-07-17T10:00:00Z",
            "Path": "docker-entrypoint.sh",
            "Args": ["postgres"],
            "RestartCount": 0,
            "Config": {
                "Env": [
                    "POSTGRES_USER=polyprop",
                    "POSTGRES_PASSWORD=fixture",
                ],
            },
            "HostConfig": {},
            "State": {
                "Status": "running",
                "StartedAt": "2026-07-17T10:01:00Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
            },
            "Mounts": [self.mount],
        }
        self.source = MEDIA.DiscoveredMedia(
            media_id="docker-volume:role-fixture",
            kind="docker_volume",
            locator="role-fixture",
            data_subpath=".",
            attached=(
                MEDIA._attached_record(
                    self.container,
                    self.mount,
                ),
            ),
            signature="postgres",
            postgres_major=16,
        )
        self.authorities = (
            {
                "name": "nexpoly",
                "oid": "16384",
                "owner": "polyprop",
                "allow_connections": True,
                "template": False,
                "audit_role": "nexpoly_auditor",
                "migration_scope": "nexpoly-ledger",
            },
            {
                "name": "postgres",
                "oid": "5",
                "owner": "polyprop",
                "allow_connections": True,
                "template": False,
                "audit_role": "nexpoly_auditor_postgres",
                "migration_scope": "adjacent-record-only",
            },
        )
        self.discovery = MEDIA.Discovery(
            media={self.source.media_id: self.source},
            docker_inventory_sha256="sha256:" + "c" * 64,
            backup_inventory_sha256="sha256:" + "d" * 64,
            scanned_volume_names=(self.source.locator,),
            scanned_bind_sources=(),
            scanned_container_ids=(CONTAINER_A,),
        )
        self.role_sql = b"SELECT 'role-contract-v2';\n"
        self.role_sql_sha256 = MEDIA.sha256_bytes(self.role_sql)
        self.system_identifier = "7312345678901234567"

    def registry(
        self,
        *,
        authorities: tuple[dict[str, object], ...] | None = None,
    ) -> MEDIA.Registry:
        records = authorities or self.authorities
        descriptor_value = MEDIA.MediaDescriptor(
            media_id=self.source.media_id,
            kind="docker_volume",
            database="nexpoly",
            database_user=str(records[0]["audit_role"]),
            disposition="read-only-online",
            audit_method="live-read-only",
            online_admin_role="polyprop",
            classification="nexpoly-db",
            source_postgres_major=16,
            databases=records,
        )
        return MEDIA.Registry(
            payload=b"registry",
            digest="sha256:" + "b" * 64,
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            descriptors=(descriptor_value,),
            required_online_databases=(),
            boundary={},
            authority_rules_sha256="sha256:" + "a" * 64,
            audit_images=((16, IMAGE),),
            audit_image_ids=((16, IMAGE_ID),),
        )

    def plan(
        self,
        *,
        registry: MEDIA.Registry | None = None,
        discovery: MEDIA.Discovery | None = None,
    ) -> dict[str, object]:
        selected_registry = registry or self.registry()
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
            for descriptor in selected_registry.descriptors
            for authority in descriptor.databases
        ]
        with (
            mock.patch.object(
                MEDIA,
                "_live_source_system_identifier",
                return_value=self.system_identifier,
            ),
            mock.patch.object(
                MEDIA,
                "_container_database_inventory",
                return_value=inventory,
            ),
            mock.patch.object(
                MEDIA,
                "_managed_role_matrix",
                return_value={
                    "matrix_sha256": "sha256:" + "9" * 64
                },
            ),
        ):
            return MEDIA.external_database_role_plan(
                selected_registry,
                discovery or self.discovery,
                role_sql_sha256=self.role_sql_sha256,
                runner=mock.Mock(),
            )

    @staticmethod
    def reseal(plan: dict[str, object]) -> None:
        unsigned = {
            key: value
            for key, value in plan.items()
            if key != "plan_sha256"
        }
        plan["plan_sha256"] = MEDIA.sha256_bytes(
            MEDIA.canonical_json_bytes(unsigned)
        )

    def test_plan_separates_dynamic_cas_from_stable_unique_role_contracts(
        self,
    ) -> None:
        plan = self.plan()
        self.assertEqual(plan["schema_version"], 2)
        self.assertEqual(
            plan["plan_sha256"],
            MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(
                    {
                        key: value
                        for key, value in plan.items()
                        if key != "plan_sha256"
                    }
                )
            ),
        )
        entries = plan["databases"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(
            len(
                {
                    (
                        value["cluster_system_identifier"],
                        value["audit_role"],
                    )
                    for value in entries
                }
            ),
            2,
        )
        for entry in entries:
            self.assertEqual(
                entry["role_contract_sha256"],
                MEDIA._external_role_contract_sha256(
                    authority_rules_sha256=(
                        plan["media_authority_rules_sha256"]
                    ),
                    role_sql_sha256=self.role_sql_sha256,
                    entry=entry,
                ),
            )
        registry = self.registry()
        with mock.patch.dict(
            os.environ,
            {
                MEDIA.AUDIT_ROLE_SQL_DIGEST_ENV: (
                    self.role_sql_sha256
                )
            },
        ):
            steady_contracts = (
                MEDIA._expected_role_contracts_for_descriptor(
                    registry,
                    registry.descriptors[0],
                    cluster_system_identifier=(
                        self.system_identifier
                    ),
                )
            )
        self.assertEqual(
            steady_contracts,
            {
                str(value["audit_role"]): str(
                    value["role_contract_sha256"]
                )
                for value in entries
            },
        )

        drifted_discovery = replace(
            self.discovery,
            docker_inventory_sha256="sha256:" + "e" * 64,
        )
        drifted = self.plan(discovery=drifted_discovery)
        self.assertNotEqual(
            plan["plan_sha256"],
            drifted["plan_sha256"],
        )
        self.assertEqual(
            [
                value["role_contract_sha256"]
                for value in plan["databases"]
            ],
            [
                value["role_contract_sha256"]
                for value in drifted["databases"]
            ],
        )

        duplicate = tuple(copy.deepcopy(self.authorities))
        duplicate[1]["audit_role"] = duplicate[0]["audit_role"]
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "cluster-global role",
        ):
            self.plan(registry=self.registry(authorities=duplicate))

    def test_provision_rejects_plan_and_contract_tamper_before_sql(
        self,
    ) -> None:
        for name, mutate, reseal in (
            (
                "plan-seal",
                lambda plan: plan["databases"][0].update(
                    {"database_owner": "other"}
                ),
                False,
            ),
            (
                "entry-contract",
                lambda plan: plan["databases"][0].update(
                    {"role_contract_sha256": "sha256:" + "f" * 64}
                ),
                True,
            ),
            (
                "duplicate-role",
                lambda plan: plan["databases"][1].update(
                    {
                        "audit_role": plan["databases"][0][
                            "audit_role"
                        ],
                        "role_contract_sha256": plan["databases"][0][
                            "role_contract_sha256"
                        ],
                        "psql_variables": plan["databases"][0][
                            "psql_variables"
                        ],
                    }
                ),
                True,
            ),
        ):
            with self.subTest(name=name):
                plan = self.plan()
                mutate(plan)
                if reseal:
                    self.reseal(plan)
                runner = mock.Mock(spec=MEDIA.CommandRunner)
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            MEDIA.AUDIT_ROLE_SQL_DIGEST_ENV: (
                                self.role_sql_sha256
                            )
                        },
                    ),
                    mock.patch.object(
                        MEDIA,
                        "_run_trusted_psql",
                    ) as execute_sql,
                    self.assertRaises(MEDIA.MediaEvidenceError),
                ):
                    MEDIA.provision_external_database_roles(
                        plan,
                        role_sql=self.role_sql,
                        runner=runner,
                    )
                execute_sql.assert_not_called()

    def test_provision_rechecks_epoch_and_verifies_exact_marker_state(
        self,
    ) -> None:
        one_database = (copy.deepcopy(self.authorities[0]),)
        plan = self.plan(
            registry=self.registry(authorities=one_database)
        )
        entry = plan["databases"][0]
        audit = database_audit(
            str(entry["database"]),
            str(entry["audit_role"]),
            system_identifier=self.system_identifier,
            database_oid=str(entry["database_oid"]),
            database_owner=str(entry["database_owner"]),
        )
        audit.pop("_unused_empty_digest")
        audit["role_contract_sha256"] = entry[
            "role_contract_sha256"
        ]
        audit["role_contract_marker"] = (
            MEDIA.ROLE_CONTRACT_POLICY
            + ":"
            + str(entry["role_contract_sha256"])
        )
        inventory = [
            {
                "name": entry["database"],
                "oid": entry["database_oid"],
                "owner": entry["database_owner"],
                "allow_connections": True,
                "template": False,
            }
        ]
        matrix_result = {
            "container_id": CONTAINER_A,
            "matrix_sha256": entry[
                "preprovision_role_matrix_sha256"
            ],
        }
        execute_sql = mock.Mock()
        with (
            mock.patch.dict(
                os.environ,
                {
                    MEDIA.AUDIT_ROLE_SQL_DIGEST_ENV: (
                        self.role_sql_sha256
                    )
                },
            ),
            mock.patch.object(
                MEDIA,
                "_optional_docker_inspect",
                return_value=self.container,
            ),
            mock.patch.object(
                MEDIA,
                "_online_container_admin_role",
                return_value="polyprop",
            ),
            mock.patch.object(
                MEDIA,
                "_container_database_inventory",
                return_value=inventory,
            ),
            mock.patch.object(
                MEDIA,
                "_live_source_system_identifier",
                return_value=self.system_identifier,
            ) as system_identifier,
            mock.patch.object(
                MEDIA,
                "_run_trusted_psql",
                execute_sql,
            ),
            mock.patch.object(
                MEDIA,
                "_audit_container_database",
                return_value=audit,
            ) as verify_role,
            mock.patch.object(
                MEDIA,
                "_managed_role_matrix",
                return_value=matrix_result,
            ),
        ):
            result = MEDIA.provision_external_database_roles(
                plan,
                role_sql=self.role_sql,
                runner=mock.Mock(spec=MEDIA.CommandRunner),
            )
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(system_identifier.call_count, 4)
        role_command = execute_sql.call_args.kwargs["arguments"]
        self.assertIn(
            (
                "role_contract_sha256="
                + str(entry["role_contract_sha256"])
            ),
            role_command,
        )
        self.assertEqual(
            verify_role.call_args.kwargs[
                "expected_role_contract_sha256"
            ],
            entry["role_contract_sha256"],
        )

        with (
            mock.patch.dict(
                os.environ,
                {
                    MEDIA.AUDIT_ROLE_SQL_DIGEST_ENV: (
                        self.role_sql_sha256
                    )
                },
            ),
            mock.patch.object(
                MEDIA,
                "_optional_docker_inspect",
                return_value=self.container,
            ),
            mock.patch.object(
                MEDIA,
                "_online_container_admin_role",
                return_value="polyprop",
            ),
            mock.patch.object(
                MEDIA,
                "_container_database_inventory",
                return_value=inventory,
            ),
            mock.patch.object(
                MEDIA,
                "_live_source_system_identifier",
                side_effect=[
                    self.system_identifier,
                    self.system_identifier,
                    "7000000000000000000",
                ],
            ),
            mock.patch.object(
                MEDIA,
                "_run_trusted_psql",
            ) as drifted_sql,
            mock.patch.object(
                MEDIA,
                "_managed_role_matrix",
                return_value=matrix_result,
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "system identifier changed",
            ),
        ):
            MEDIA.provision_external_database_roles(
                plan,
                role_sql=self.role_sql,
                runner=mock.Mock(spec=MEDIA.CommandRunner),
            )
        self.assertEqual(drifted_sql.call_count, 1)

        with (
            mock.patch.dict(
                os.environ,
                {
                    MEDIA.AUDIT_ROLE_SQL_DIGEST_ENV: (
                        self.role_sql_sha256
                    )
                },
            ),
            mock.patch.object(
                MEDIA,
                "_optional_docker_inspect",
                return_value=self.container,
            ),
            mock.patch.object(
                MEDIA,
                "_online_container_admin_role",
                return_value="polyprop",
            ),
            mock.patch.object(
                MEDIA,
                "_container_database_inventory",
                return_value=inventory,
            ),
            mock.patch.object(
                MEDIA,
                "_live_source_system_identifier",
                return_value=self.system_identifier,
            ),
            mock.patch.object(
                MEDIA,
                "_run_trusted_psql",
            ) as unsafe_sql,
            mock.patch.object(
                MEDIA,
                "_audit_container_database",
                side_effect=MEDIA.MediaEvidenceError(
                    "audit role direct ACL differs"
                ),
            ),
            mock.patch.object(
                MEDIA,
                "_managed_role_matrix",
                return_value=matrix_result,
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "direct ACL differs",
            ),
        ):
            MEDIA.provision_external_database_roles(
                plan,
                role_sql=self.role_sql,
                runner=mock.Mock(spec=MEDIA.CommandRunner),
            )
        self.assertEqual(unsafe_sql.call_count, 1)


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
            online_admin_role="polyprop",
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
            mock.patch.object(
                MEDIA,
                "_managed_role_matrix",
                return_value={
                    "matrix_sha256": "sha256:" + "9" * 64
                },
            ),
        ):
            bundle = MEDIA._audit_container_medium(
                MEDIA.CommandRunner(),
                CONTAINER_A,
                current,
                isolated=False,
                expected_data_directory="/var/lib/postgresql/data",
                trusted_image_id=IMAGE_ID,
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

    def test_live_audit_uses_pinned_external_tcp_client(self) -> None:
        authority = self.authority("nexpoly", "16384")
        current = MEDIA.MediaDescriptor(
            media_id="docker-volume:fixture",
            kind="docker_volume",
            database="nexpoly",
            database_user="auditor",
            disposition="read-only-online",
            audit_method="live-read-only",
            online_admin_role="polyprop",
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
                    "Config": {
                        "Env": [
                            "POSTGRES_USER=polyprop",
                            "POSTGRES_PASSWORD=must-never-be-recorded",
                            "POSTGRES_HOST_AUTH_METHOD=trust",
                        ],
                    },
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
                if (
                    values[1] == "run"
                    and "--entrypoint" in values
                    and "psql" in values
                ):
                    sql_input = (input_bytes or b"").split(b"\n", 1)[-1]
                    if sql_input == MEDIA._database_inventory_sql_for_major(
                        16
                    ).encode():
                        admin = {
                            "record_type": "online_admin",
                            "session_user": "polyprop",
                            "current_user": "polyprop",
                            "role_superuser": True,
                            "role_can_login": True,
                        }
                        inventory = {
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
                        stdout = (
                            json.dumps(admin).encode()
                            + b"\n"
                            + json.dumps(inventory).encode()
                            + b"\n"
                        )
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
        with (
            mock.patch.object(
                MEDIA,
                "_trusted_server_runtime_epoch",
                return_value={"fixture": "stable"},
            ),
            mock.patch.object(
                MEDIA,
                "_local_audit_image_id",
                return_value=IMAGE_ID,
            ),
            mock.patch.object(
                MEDIA,
                "_trusted_server_startup_projection",
                return_value=trusted_startup_projection(),
            ),
            mock.patch.object(
                MEDIA,
                "_managed_role_matrix",
                return_value={
                    "matrix_sha256": "sha256:" + "9" * 64
                },
            ),
        ):
            bundle = MEDIA._run_live_audit(
                runner,
                current,
                source,
                trusted_image_id=IMAGE_ID,
            )
        self.assertEqual(bundle["database_identity"]["database"], "nexpoly")
        psql_commands = [
            values
            for values, _payload in runner.commands
            if values[1] == "run" and "psql" in values
        ]
        self.assertEqual(len(psql_commands), 2)
        for command in psql_commands:
            self.assertIn(
                f"container:{CONTAINER_A}",
                command,
            )
            self.assertIn(
                f"PGOPTIONS={MEDIA.PSQL_AUDIT_PGOPTIONS}",
                command,
            )
            self.assertIn(IMAGE, command)
            self.assertIn("--read-only", command)
            self.assertIn("--cap-drop", command)
            self.assertIn("no-new-privileges:true", command)
            self.assertIn("127.0.0.1", command)
            self.assertNotIn("must-never-be-recorded", command)
            self.assertNotIn("/var/run/postgresql", command)

    def test_live_descriptor_fails_closed_for_disabled_database(
        self,
    ) -> None:
        source = MEDIA.DiscoveredMedia(
            media_id="docker-volume:fixture",
            kind="docker_volume",
            locator="fixture",
            data_subpath=".",
            attached=(attachment_record(CONTAINER_A),),
            signature="postgres",
            postgres_major=16,
        )
        authority = MEDIA.MediaAuthorityRules(
            payload=b"fixture",
            digest="sha256:" + "1" * 64,
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            descriptors=(),
            required_online_databases=(),
            policy=MEDIA.DiscoveryPolicy(),
            allow_unmatched_non_postgres=False,
            production_identity={},
            audit_images=tuple(
                sorted(MEDIA.POSTGRES_AUDIT_IMAGES.items())
            ),
        )
        inventory = [
            {
                "name": "nexpoly",
                "oid": "16384",
                "owner": "postgres",
                "allow_connections": True,
                "template": False,
            },
            {
                "name": "hidden_legacy",
                "oid": "16385",
                "owner": "postgres",
                "allow_connections": False,
                "template": False,
            },
        ]
        with (
            mock.patch.object(
                MEDIA,
                "_active_media_attachments",
                return_value=list(source.attached),
            ),
            mock.patch.object(
                MEDIA,
                "_container_database_inventory",
                return_value=inventory,
            ),
            mock.patch.object(
                MEDIA,
                "_online_container_admin_role",
                return_value="polyprop",
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "non-connectable.*hidden_legacy",
            ),
        ):
            MEDIA._live_runtime_descriptor(
                authority,
                source,
                primary_database="nexpoly",
                audit_role="nexpoly_auditor",
                disposition="writable-target",
                runner=MEDIA.CommandRunner(),
                audit_image_id=IMAGE_ID,
            )


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

    def test_reviewed_read_only_bind_never_exposes_source_to_docker(
        self,
    ) -> None:
        path = self.root / "reviewed-bind"
        private_directory(path)
        private_file(path / "settings.ini", b"[fixture]\nenabled=true\n")
        os.chmod(path / "settings.ini", 0o444)
        os.chmod(path, 0o555)
        media_id = f"container-bind:{path}"
        current = MEDIA.MediaDescriptor(
            media_id=media_id,
            kind="container_bind",
            database="none",
            database_user="none",
            disposition="excluded-from-nexpoly-migration",
            audit_method="reviewed-content-only",
            classification="reviewed-non-pg",
            source_postgres_major=None,
        )
        source = MEDIA.DiscoveredMedia(
            media_id=media_id,
            kind="container_bind",
            locator=str(path),
            data_subpath=".",
            attached=(),
            signature="non-postgres",
            postgres_major=None,
        )

        class NoDockerOperation:
            workspace = self.workspace

            def run_container(self, *_args, **_kwargs):
                raise AssertionError(
                    "reviewed host bind was passed to Docker"
                )

        with mock.patch.object(
            MEDIA,
            "_current_attachments",
            return_value=[],
        ):
            private_review = MEDIA._review_non_postgres_bind(
                self.registry,
                source,
                runner=MEDIA.CommandRunner(),
                operation=NoDockerOperation(),
                resource_prefix="reviewed-bind",
            )
            record = MEDIA._record_only_medium(
                MEDIA.CommandRunner(),
                self.registry,
                current,
                source,
                self.workspace,
                policy=MEDIA.DiscoveryPolicy(
                    backup_roots=(self.root,)
                ),
                operation=NoDockerOperation(),
                resource_prefix="medium-record",
                auditor_sha256="sha256:" + "6" * 64,
                audit_image_id="sha256:" + "5" * 64,
                audited_at="2026-07-17T12:00:00Z",
            )
        self.assertEqual(
            record["source_content_sha256"],
            private_review["source_content_sha256"],
        )
        self.assertFalse(
            record["audit"]["isolation"]["source_passed_to_docker"]
        )
        CONTRACTS._external_record_only_medium_v3(
            record,
            volume_names=[],
            container_ids=[],
        )

    def test_reviewed_single_file_bind_rejects_backup_magic(self) -> None:
        path = self.root / "hidden-config"
        private_file(path, b"PGDMP" + b"\0" * 128)
        source = MEDIA.DiscoveredMedia(
            media_id=f"container-bind:{path}",
            kind="container_bind",
            locator=str(path),
            data_subpath=".",
            attached=(),
            signature="non-postgres",
            postgres_major=None,
        )
        with (
            mock.patch.object(
                MEDIA,
                "_current_attachments",
                return_value=[],
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "backup material",
            ),
        ):
            MEDIA._review_non_postgres_bind(
                self.registry,
                source,
                runner=MEDIA.CommandRunner(),
                operation=self.operation,
                resource_prefix="backup-magic",
            )

    def test_reviewed_bind_ctime_detects_same_length_mtime_restore(
        self,
    ) -> None:
        path = self.root / "ctime-bind"
        private_file(path, b"A" * 128)
        original = path.stat()
        real_read = os.read
        calls = 0

        def mutate_after_payload(descriptor: int, size: int) -> bytes:
            nonlocal calls
            calls += 1
            if calls == 2:
                with path.open("r+b", buffering=0) as target:
                    target.write(b"B" * 128)
                    target.flush()
                    os.fsync(target.fileno())
                os.utime(
                    path,
                    ns=(original.st_atime_ns, original.st_mtime_ns),
                )
            return real_read(descriptor, size)

        with (
            mock.patch.object(
                MEDIA.os,
                "read",
                side_effect=mutate_after_payload,
            ),
            self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "changed during scan",
            ),
        ):
            MEDIA._scan_reviewed_bind_tree(path)

    def test_adjacent_pg18_volume_is_never_started_as_postgres(self) -> None:
        media_id = "docker-volume:adjacent-pg18"
        current = MEDIA.MediaDescriptor(
            media_id=media_id,
            kind="docker_volume",
            database="none",
            database_user="none",
            disposition="excluded-from-nexpoly-migration",
            audit_method="adjacent-record-only",
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
        self.probed_binds: list[str] = []

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
        if values[1] == "exec" and "/bin/sh" in values:
            container_id = (
                values[4] if values[2:4] == ["--user", "postgres"]
                else values[2]
            )
            script = values[-1]
            match = re.search(r"root=([^;]+);", script)
            if match is None:
                raise AssertionError(
                    f"fake marker probe lacks a root: {values!r}"
                )
            root = shlex.split(match.group(1))[0]
            container = self.containers[container_id]
            mounts = container["Mounts"]
            mount = next(
                value
                for value in mounts
                if root == value["Destination"]
                or root.startswith(str(value["Destination"]) + "/")
            )
            source = (
                mount["Name"]
                if mount["Type"] == "volume"
                else mount["Source"]
            )
            output = self.probe.get(str(source), "")
            if "find \"$root\"" in script:
                output = output.replace("/source", str(mount["Destination"]))
            else:
                version = re.search(r"(?m)^V\t[^\t]+\t([0-9]+)$", output)
                if version is not None:
                    output = version.group(1)
                elif mount["Type"] == "bind":
                    relative = PurePosixPath(root).relative_to(
                        PurePosixPath(str(mount["Destination"]))
                    )
                    version_file = Path(str(mount["Source"])).joinpath(
                        *relative.parts,
                        "PG_VERSION",
                    )
                    output = (
                        version_file.read_text(encoding="ascii").strip()
                        if version_file.is_file()
                        else ""
                    )
                else:
                    output = ""
            return self.complete(values, output.encode())
        if values[1] == "run":
            mount = next(
                value
                for value in values
                if isinstance(value, str)
                and (
                    value.startswith("type=volume,src=")
                    or value.startswith("type=bind,src=")
                )
            )
            source = mount.split(",")[1].split("=", 1)[1]
            if mount.startswith("type=volume,"):
                self.probed.append(source)
                output = self.probe.get(source, "")
            else:
                self.probed_binds.append(source)
                path = Path(source)
                output = ""
                if path.is_dir():
                    for version in sorted(path.rglob("PG_VERSION")):
                        cluster = version.parent
                        if (
                            (cluster / "global/pg_control").is_file()
                            and (cluster / "base").is_dir()
                        ):
                            relative = cluster.relative_to(path)
                            mounted = (
                                "/source"
                                if not relative.parts
                                else f"/source/{relative.as_posix()}"
                            )
                            output = postgres_signature(mounted)
                            break
            return self.complete(values, output.encode())
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
        self.image_ids = {
            14: "sha256:" + "4" * 64,
            15: "sha256:" + "6" * 64,
            16: self.image_id,
            18: "sha256:" + "8" * 64,
        }
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
            image = values[-1]
            major = next(
                major
                for major, candidate in MEDIA.POSTGRES_AUDIT_IMAGES.items()
                if candidate == image
            )
            return FakeDockerRunner.complete(
                values,
                json.dumps(
                    [
                        {
                            "Id": self.image_ids[major],
                            "RepoDigests": [image],
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
            image_index = next(
                index
                for index, value in enumerate(values)
                if value in MEDIA.POSTGRES_AUDIT_IMAGES.values()
            )
            selected_image = values[image_index]
            selected_major = next(
                major
                for major, candidate in MEDIA.POSTGRES_AUDIT_IMAGES.items()
                if candidate == selected_image
            )
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
                image=selected_image,
                image_id=self.image_ids[selected_major],
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
                output = (
                    f"postgres (PostgreSQL) {selected_major}.4\n"
                )
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

        self.assertEqual(result["signature"], "postgres")
        self.assertEqual(result["data_subpath"], ".")
        self.assertEqual(result["postgres_major"], 16)
        self.assertEqual(
            result["classification_probe"]["result"],
            "complete-postgres-signature",
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
        self.assertEqual(
            set(runner.probed),
            {production, development, dormant, unrelated},
        )

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
            descriptors=(MEDIA.MediaDescriptor(**reviewed),),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(self.policy),
        )

        runner = FakeDockerRunner(
            [container],
            [self.volume(name)],
            {name: ""},
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "PGDATA conflicts|PG_VERSION is invalid",
        ):
            MEDIA.discover_media(
                registry,
                runner=runner,
                operation=PassthroughScratchOperation(runner, self.root),
                policy=self.policy,
            )

        container["State"]["Status"] = "running"
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "PGDATA conflicts|PG_VERSION is invalid",
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
            "partial or ambiguous PostgreSQL markers|PGDATA conflicts",
        ):
            MEDIA.discover_media(
                registry,
                runner=runner,
                operation=PassthroughScratchOperation(runner, self.root),
                policy=self.policy,
            )

    def test_read_only_disjoint_single_file_binds_are_reviewed_media(
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
        init_source = self.root / "private/init/10-schema.sql"
        init_source.parent.mkdir(parents=True, mode=0o700)
        os.chmod(init_source.parent, 0o700)
        private_file(init_source, b"CREATE SCHEMA fixture;\n")
        init_bind = {
            "Type": "bind",
            "Source": str(init_source),
            "Destination": "/docker-entrypoint-initdb.d/10-schema.sql",
            "RW": False,
        }
        container["Mounts"].append(init_bind)
        media_id = f"docker-volume:{name}"
        bind_media_id = f"container-bind:{init_source}"
        registry = MEDIA.Registry(
            payload=b"fixture",
            digest=MEDIA.sha256_bytes(b"fixture"),
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
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
                MEDIA.MediaDescriptor(
                    **{
                        **descriptor(
                            bind_media_id,
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
        self.assertEqual(
            sorted(result.media),
            sorted([media_id, bind_media_id]),
        )
        self.assertEqual(
            result.media[bind_media_id].signature,
            "non-postgres",
        )
        self.assertEqual(
            result.scanned_bind_sources,
            (str(init_source),),
        )

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
                    (
                        "unclassified persistent bind"
                        "|masked by an overlapping mount"
                    ),
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
        media_id = f"docker-volume:{name}"
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
        self.assertEqual(runner.probed, [name])

        no_markers = FakeDockerRunner(
            [container],
            [self.volume(name)],
            {name: ""},
        )
        discovery = MEDIA.discover_media(
            registry,
            runner=no_markers,
            operation=PassthroughScratchOperation(
                no_markers,
                self.root,
            ),
            policy=self.policy,
            enforce_registry=False,
        )
        self.assertEqual(discovery.media[media_id].signature, "non-postgres")
        self.assertEqual(
            discovery.media[media_id].classification_probe["method"],
            "bounded-postgres-marker-probe-v1",
        )
        self.assertEqual(no_markers.probed, [name])

    def test_generic_data_directory_flags_do_not_prove_postgres(self) -> None:
        container = {
            "Path": "/usr/local/bin/ordinary-service",
            "Args": ["-D", "/srv/state", "-cdata_directory=/srv/other"],
            "Config": {
                "Image": "private/ordinary-service:1",
                "Env": ["PGDATA=/srv/third"],
                "Labels": {},
                "Entrypoint": ["/usr/local/bin/ordinary-service"],
                "Cmd": ["-D", "/srv/state"],
            },
            "Mounts": [],
        }
        self.assertEqual(
            MEDIA._container_workload_classification(container),
            "unknown",
        )
        self.assertIsNone(MEDIA._container_pgdata(container))

    def test_active_minio_volume_is_metadata_only_without_mounting(self) -> None:
        name = "business-object-store"
        mount = {
            "Type": "volume",
            "Name": name,
            "Source": name,
            "Destination": "/data",
            "RW": True,
        }
        container = self.container(
            CONTAINER_A,
            database_mount=mount,
            pgdata="/unused",
        )
        container["Path"] = "minio"
        container["Args"] = ["server", "/data"]
        container["Config"]["Image"] = "minio:latest"
        container["Config"]["Env"] = []
        container["Config"]["Entrypoint"] = ["minio"]
        container["Config"]["Cmd"] = ["server", "/data"]
        registry = MEDIA.Registry(
            payload=b"fixture",
            digest=MEDIA.sha256_bytes(b"fixture"),
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            descriptors=(),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(self.policy),
        )
        runner = FakeDockerRunner(
            [container],
            [self.volume(name)],
            {},
        )
        discovery = MEDIA.discover_media(
            registry,
            runner=runner,
            operation=PassthroughScratchOperation(runner, self.root),
            policy=self.policy,
            enforce_registry=False,
        )
        source = discovery.media[f"docker-volume:{name}"]
        self.assertEqual(source.signature, "non-postgres")
        self.assertEqual(
            source.classification_probe,
            {
                "method": "bounded-postgres-marker-probe-v1",
                "maximum_entries": (
                    MEDIA.MAX_POSTGRES_MARKER_PROBE_ENTRIES
                ),
                "maximum_marker_results": (
                    MEDIA.MAX_POSTGRES_MARKER_RESULTS
                ),
                "timeout_seconds": (
                    MEDIA.POSTGRES_MARKER_PROBE_TIMEOUT_SECONDS
                ),
                "result": "no-postgres-markers",
                "content_read_scope": "PG_VERSION-up-to-32-bytes",
            },
        )
        self.assertEqual(runner.probed, [name])

        hostile = FakeDockerRunner(
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
                runner=hostile,
                operation=PassthroughScratchOperation(
                    hostile,
                    self.root,
                ),
                policy=self.policy,
                enforce_registry=False,
            )

    def test_image_metadata_alone_cannot_hide_an_active_unknown_reader(
        self,
    ) -> None:
        container = self.container(
            CONTAINER_A,
            database_mount={
                "Type": "volume",
                "Name": "spoofed-service-data",
                "Source": "spoofed-service-data",
                "Destination": "/data",
                "RW": True,
            },
            pgdata="/data",
        )
        container["Path"] = "/bin/sh"
        container["Args"] = ["-c", "sleep infinity"]
        container["Config"]["Image"] = "redis:latest"
        container["Config"]["Labels"] = {
            "org.opencontainers.image.title": "Redis"
        }
        container["Config"]["Entrypoint"] = ["/bin/sh"]
        container["Config"]["Cmd"] = ["-c", "sleep infinity"]
        self.assertEqual(
            MEDIA._container_workload_classification(container),
            "unknown",
        )

    def test_active_volume_subpath_is_rejected_before_partial_scan(
        self,
    ) -> None:
        name = "mixed-volume"
        mount = {
            "Type": "volume",
            "Name": name,
            "Source": name,
            "Destination": "/data",
            "RW": True,
        }
        container = self.container(
            CONTAINER_A,
            database_mount=mount,
            pgdata="/unused",
        )
        container["Path"] = "minio"
        container["Args"] = ["server", "/data"]
        container["Config"]["Image"] = "minio:latest"
        container["Config"]["Env"] = []
        container["HostConfig"] = {
            "Mounts": [
                {
                    "Type": "volume",
                    "Source": name,
                    "Target": "/data",
                    "VolumeOptions": {"Subpath": "object-store"},
                }
            ]
        }
        registry = MEDIA.Registry(
            payload=b"fixture",
            digest=MEDIA.sha256_bytes(b"fixture"),
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
            descriptors=(),
            required_online_databases=(),
            boundary=MEDIA.seal_discovery_boundary(self.policy),
        )
        runner = FakeDockerRunner(
            [container],
            [self.volume(name)],
            {name: postgres_signature("/source/hidden-pgdata")},
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "volume subpath",
        ):
            MEDIA.discover_media(
                registry,
                runner=runner,
                operation=PassthroughScratchOperation(
                    runner,
                    self.root,
                ),
                policy=self.policy,
                enforce_registry=False,
            )

    def test_active_bind_identity_accepts_postgres_owned_private_root(
        self,
    ) -> None:
        path = self.root / "postgres-owned-pgdata"
        metadata = types.SimpleNamespace(
            st_dev=11,
            st_ino=12,
            st_mtime_ns=13,
            st_ctime_ns=14,
            st_mode=stat.S_IFDIR | 0o700,
            st_uid=70,
            st_gid=70,
        )
        source = MEDIA.DiscoveredMedia(
            media_id=f"container-bind:{path}",
            kind="container_bind",
            locator=str(path),
            data_subpath=".",
            attached=(attachment_record(CONTAINER_A),),
            signature="postgres",
            postgres_major=16,
        )
        with mock.patch.object(
            MEDIA.os,
            "lstat",
            side_effect=(metadata, metadata),
        ):
            identity = MEDIA._live_source_identity(
                MEDIA.CommandRunner(),
                source,
            )
        self.assertEqual(identity["uid"], 70)
        self.assertEqual(identity["ctime_ns"], 14)

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
            (
                "unclassified persistent volume without PG_VERSION"
                "|PostgreSQL signature escaped its root"
                "|masked by an overlapping mount"
            ),
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
        container["Config"]["Env"].extend(
            [
                "POSTGRES_USER=postgres",
                "POSTGRES_HOST_AUTH_METHOD=trust",
            ]
        )

        class LiveIdentityRunner(FakeDockerRunner):
            def run(self, arguments, **kwargs):
                values = list(arguments)
                if (
                    values[1] == "run"
                    and "--entrypoint" in values
                    and "psql" in values
                ):
                    self.assert_client = values
                    return self.complete(
                        values,
                        b"7312345678901234567\n",
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
        with (
            mock.patch.object(
                MEDIA,
                "_trusted_server_runtime_epoch",
                return_value={"fixture": "stable"},
            ),
            mock.patch.object(
                MEDIA,
                "_local_audit_image_id",
                return_value=IMAGE_ID,
            ),
            mock.patch.object(
                MEDIA,
                "_trusted_server_startup_projection",
                return_value={"fixture": "stable"},
            ),
        ):
            self.assertEqual(
                MEDIA._live_source_system_identifier(
                    runner,
                    source,
                    trusted_image_id=IMAGE_ID,
                ),
                "7312345678901234567",
            )
        self.assertIn(f"container:{CONTAINER_A}", runner.assert_client)
        self.assertIn("127.0.0.1", runner.assert_client)
        self.assertNotIn("/srv/database/pgdata", runner.assert_client)


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

    def test_multi_major_authority_binds_pg18_container_to_exact_image_id(
        self,
    ) -> None:
        authority = {
            **self.authority,
            "postgres_images": {
                "16": {
                    "digest_ref": MEDIA.POSTGRES_AUDIT_IMAGES[16],
                    "image_id": self.runner.image_ids[16],
                },
                "18": {
                    "digest_ref": MEDIA.POSTGRES_AUDIT_IMAGES[18],
                    "image_id": self.runner.image_ids[18],
                },
            },
        }
        operation = MEDIA.ScratchOperation.begin(
            self.evidence,
            runner=self.runner,
            authority=authority,
        )
        completed = operation.run_container(
            "pg18-version",
            [
                MEDIA.DOCKER,
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "postgres",
                MEDIA.POSTGRES_AUDIT_IMAGES[18],
                "--version",
            ],
        )
        self.assertIn(b"18.4", completed.stdout)
        resource = operation.journal["resources"][0]
        self.assertEqual(
            resource["spec"]["postgres_image"],
            MEDIA.POSTGRES_AUDIT_IMAGES[18],
        )
        self.assertEqual(
            resource["spec"]["postgres_image_id"],
            self.runner.image_ids[18],
        )
        self.assertEqual(
            resource["spec"]["tmpfs"],
            {
                "/var/lib/postgresql": (
                    "rw,noexec,nosuid,size=1m,mode=0700"
                )
            },
        )
        self.assertNotIn(
            "/var/lib/postgresql/data",
            resource["spec"]["tmpfs"],
        )
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "exactly one authority image",
        ):
            operation.run_container(
                "pg15-version",
                [
                    MEDIA.DOCKER,
                    "run",
                    "--rm",
                    MEDIA.POSTGRES_AUDIT_IMAGES[15],
                    "--version",
                ],
            )
        operation.abort()

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
            mock.patch.object(
                MEDIA,
                "DEFAULT_EVIDENCE_ROOT",
                self.evidence,
            ),
            mock.patch.object(sys, "stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(
                MEDIA.main(
                    [
                        "recover",
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
            descriptors=(),
            required_online_databases=(),
            boundary={},
        )
        discovery = MEDIA.Discovery(
            media={},
            docker_inventory_sha256="sha256:" + "7" * 64,
            backup_inventory_sha256="sha256:" + "8" * 64,
            scanned_volume_names=(),
            scanned_bind_sources=(),
            scanned_container_ids=(),
        )
        discovery_observed: list[bool] = []
        authority_rules = MEDIA.MediaAuthorityRules(
            payload=b"rules",
            digest="sha256:" + "9" * 64,
            audit_image=IMAGE,
            auditor_sha256=registry.auditor_sha256,
            descriptors=(),
            required_online_databases=(),
            policy=MEDIA.DiscoveryPolicy(backup_roots=()),
            allow_unmatched_non_postgres=False,
            production_identity={
                "stack": "production",
                "database": "nexpoly",
                "kind": "docker_volume",
                "media_id": "docker-volume:nexpoly_app_postgres_data",
                "postgres_major": 16,
                "system_identifier": "7659245354718314530",
            },
        )

        def generate_after_recovery(*_args, **_kwargs):
            discovery_observed.append(stale_name not in self.runner.volumes)
            return registry, discovery

        with (
            mock.patch.object(
                MEDIA,
                "CommandRunner",
                return_value=self.runner,
            ),
            mock.patch.object(
                MEDIA,
                "DEFAULT_EVIDENCE_ROOT",
                self.evidence,
            ),
            mock.patch.object(
                MEDIA,
                "load_authority_rules",
                return_value=authority_rules,
            ),
            mock.patch.object(
                MEDIA,
                "_local_audit_image_id",
                return_value=self.authority["postgres_image_id"],
            ),
            mock.patch.object(MEDIA, "_validate_audit_image"),
            mock.patch.object(
                MEDIA,
                "generate_runtime_registry",
                side_effect=generate_after_recovery,
            ),
            mock.patch.object(
                MEDIA,
                "build_evidence",
                return_value={"schema_version": 2, "fixture": True},
            ),
            mock.patch.dict(
                os.environ,
                {
                    "NEXPOLY_CONTRACT_0012_MEDIA_AUTHORITY_RULES_SHA256": (
                        authority_rules.digest
                    )
                },
            ),
        ):
            result = MEDIA.main(["build"])
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
        "server_startup": audited_startup_fields(data_directory),
        "role_superuser": False,
        "role_create_db": False,
        "role_create_role": False,
        "role_replication": False,
        "role_bypass_rls": False,
        "role_inherit": False,
        "role_can_login": False,
        "role_contract_marker": (
            MEDIA.ROLE_CONTRACT_POLICY + ":sha256:" + "a" * 64
        ),
        "role_contract_sha256": "sha256:" + "a" * 64,
        **role_security_fields(database, superuser=False),
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
        "generation_schema": {
            "state": "absent",
            "schema_sha256": None,
            "schema_authority": None,
        },
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
        for resource in self.operation.journal["resources"]:
            spec = resource.get("spec")
            if not isinstance(spec, dict):
                continue
            for mount in spec.get("mounts", []):
                self.assertNotEqual(
                    mount.get("source"),
                    str(source_path),
                    "the real host bind path must never be mounted in Docker",
                )

    def test_bind_snapshot_rejects_same_size_nested_mutation_with_mtime_restore(
        self,
    ) -> None:
        source = self.root / "bind-cas-mtime"
        private_directory(source)
        private_directory(source / "nested")
        target = source / "nested/data"
        private_file(target, b"original-content")
        target_inode = target.stat().st_ino
        original_times = (
            target.stat().st_atime_ns,
            target.stat().st_mtime_ns,
        )
        original_read = os.read
        mutated = False

        def mutate_after_read(descriptor: int, size: int) -> bytes:
            nonlocal mutated
            payload = original_read(descriptor, size)
            if (
                not mutated
                and payload
                and os.fstat(descriptor).st_ino == target_inode
            ):
                mutated = True
                time.sleep(0.01)
                with target.open("r+b", buffering=0) as stream:
                    stream.write(b"changed!-content")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.utime(target, ns=original_times)
            return payload

        with mock.patch.object(
            MEDIA.os,
            "read",
            side_effect=mutate_after_read,
        ):
            with self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "file changed",
            ):
                MEDIA._bind_tree_snapshot(
                    source,
                    self.root / "bind-cas-mtime-snapshot",
                )
        self.assertTrue(mutated)

    def test_bind_snapshot_rejects_nested_inode_swap(self) -> None:
        source = self.root / "bind-cas-swap"
        private_directory(source)
        private_directory(source / "nested")
        target = source / "nested/data"
        private_file(target, b"original-content")
        replacement = source / "nested/replacement"
        private_file(replacement, b"original-content")
        target_inode = target.stat().st_ino
        original_read = os.read
        swapped = False

        def swap_after_read(descriptor: int, size: int) -> bytes:
            nonlocal swapped
            payload = original_read(descriptor, size)
            if (
                not swapped
                and payload
                and os.fstat(descriptor).st_ino == target_inode
            ):
                swapped = True
                os.replace(replacement, target)
            return payload

        with mock.patch.object(
            MEDIA.os,
            "read",
            side_effect=swap_after_read,
        ):
            with self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "file changed",
            ):
                MEDIA._bind_tree_snapshot(
                    source,
                    self.root / "bind-cas-swap-snapshot",
                )
        self.assertTrue(swapped)

    def test_takeover_redirect_scans_exact_stopped_legacy_bind_seal(
        self,
    ) -> None:
        repository = self.root / "legacy-repo"
        archive = self.root / "takeover-archive"
        private_directory(repository)
        private_directory(archive)
        original = repository / "Backend Data"
        destination = archive / "Backend Data"
        destination.mkdir(mode=0o775)
        os.chmod(destination, 0o775)
        payload = b"legacy-runtime-data\n"
        (destination / "settings.ini").write_bytes(payload)
        os.chmod(destination / "settings.ini", 0o664)
        (destination / "current").symlink_to("settings.ini")
        records = [
            {
                "path": ".",
                "type": "directory",
                "mode": "0775",
                "uid": os.geteuid(),
                "gid": os.getegid(),
            },
            {
                "path": "current",
                "type": "symlink",
                "mode": "0777",
                "uid": os.geteuid(),
                "gid": os.getegid(),
                "target": "settings.ini",
            },
            {
                "path": "settings.ini",
                "type": "file",
                "mode": "0664",
                "uid": os.geteuid(),
                "gid": os.getegid(),
                "size": len(payload),
                "sha256": MEDIA.sha256_bytes(payload),
            },
        ]
        unsigned = {"schema_version": 1, "records": records}
        seal = {
            **unsigned,
            "digest": MEDIA.sha256_bytes(
                MEDIA.canonical_json_bytes(unsigned)
            ),
        }
        operation = {
            "moves": [
                {
                    "index": 0,
                    "path": "Backend Data",
                    "class": "runtime",
                    "destination": str(destination),
                    "seal": seal,
                }
            ]
        }
        stage = {
            "operation_id": "takeover-fixture-0001",
            "operation_state_sha256": "sha256:" + "1" * 64,
            "legacy_stopped_container_ids": [
                CONTAINER_A,
                CONTAINER_B,
            ],
        }
        attachments = [
            attachment_record(CONTAINER_A, state="exited")
        ]
        with mock.patch.object(
            MEDIA,
            "LEGACY_PRE_TAKEOVER_BACKUP_ROOT",
            repository / "backups",
        ):
            redirected = MEDIA._takeover_bind_redirect(
                str(original),
                attachments,
                operation,
                stage,
            )
        self.assertIsNotNone(redirected)
        audit_path, projected, evidence = redirected
        self.assertEqual(audit_path, destination)
        self.assertEqual(evidence["source_root"], str(original))
        scanned = MEDIA._scan_reviewed_bind_tree(
            audit_path,
            expected_takeover_seal=projected,
        )
        self.assertEqual(scanned["signature"], "non-postgres")
        self.assertFalse(scanned["contains_backup_material"])

        (destination / "settings.ini").write_bytes(
            b"tamper-runtime-data\n"
        )
        os.chmod(destination / "settings.ini", 0o664)
        with self.assertRaisesRegex(
            MEDIA.MediaEvidenceError,
            "differs from its move seal",
        ):
            MEDIA._scan_reviewed_bind_tree(
                audit_path,
                expected_takeover_seal=projected,
            )

    def test_takeover_redirect_rejects_active_or_unsealed_bind(self) -> None:
        repository = self.root / "legacy-repo-reject"
        destination = self.root / "legacy-archive-reject"
        private_directory(repository)
        private_directory(destination)
        original = repository / "model"
        metadata = destination.stat()
        records = [
            {
                "path": ".",
                "type": "directory",
                "mode": f"{metadata.st_mode & 0o7777:04o}",
                "uid": metadata.st_uid,
                "gid": metadata.st_gid,
            }
        ]
        unsigned = {"schema_version": 1, "records": records}
        operation = {
            "moves": [
                {
                    "index": 0,
                    "path": "model",
                    "class": "asset",
                    "destination": str(destination),
                    "seal": {
                        **unsigned,
                        "digest": MEDIA.sha256_bytes(
                            MEDIA.canonical_json_bytes(unsigned)
                        ),
                    },
                }
            ]
        }
        stage = {
            "operation_id": "takeover-fixture-0001",
            "operation_state_sha256": "sha256:" + "1" * 64,
            "legacy_stopped_container_ids": [CONTAINER_A],
        }
        with mock.patch.object(
            MEDIA,
            "LEGACY_PRE_TAKEOVER_BACKUP_ROOT",
            repository / "backups",
        ):
            with self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "exact stopped legacy readers",
            ):
                MEDIA._takeover_bind_redirect(
                    str(original),
                    [attachment_record(CONTAINER_A, state="running")],
                    operation,
                    stage,
                )
            self.assertIsNone(
                MEDIA._takeover_bind_redirect(
                    str(repository / "unsealed"),
                    [attachment_record(CONTAINER_A, state="exited")],
                    operation,
                    stage,
                )
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
        )
        workspace = self.operation.workspace / "backup-medium"
        workspace.mkdir(mode=0o700)
        policy = MEDIA.DiscoveryPolicy(backup_roots=(backups,))
        registry = MEDIA.Registry(
            payload=self.registry.payload,
            digest=self.registry.digest,
            audit_image=self.registry.audit_image,
            auditor_sha256=self.registry.auditor_sha256,
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
            "role_replication": isolated,
            "role_bypass_rls": isolated,
            "role_inherit": isolated,
            "role_can_login": isolated,
            **role_security_fields(
                database,
                superuser=isolated,
                ledger_present=ledger_relation["state"] == "present",
                legacy_present=legacy_present,
            ),
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
                "online_admin_role": (
                    None if isolated else "polyprop"
                ),
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
        "ctime_ns": 5,
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
            "scanned_bind_sources": [],
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
        value["server_startup"] = audited_startup_fields(
            "/var/lib/postgresql/data",
            online=not isolated,
        )
        if isolated:
            value["role_contract_marker"] = None
            value["role_contract_sha256"] = None
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
                "role_replication": isolated,
                "role_bypass_rls": isolated,
                "role_inherit": isolated,
                "role_can_login": isolated,
                **role_security_fields(
                    database,
                    superuser=isolated,
                    ledger_present=True,
                    legacy_present=legacy_present,
                ),
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
            generation_authority = {
                "owner": user,
                "acl": [],
                "comments": [],
                "security_labels": [],
                "default_acl": [],
                "initial_privileges": [],
                "publications": [],
                "unapproved_dependents": [],
            }
            value["generation_schema"] = {
                "state": "present",
                "schema_sha256": MEDIA.sha256_bytes(
                    MEDIA.canonical_json_bytes(generation_authority)
                ),
                "schema_authority": generation_authority,
            }
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
                "online_admin_role": (
                    None if isolated else "polyprop"
                ),
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
                        "server_startup",
                        "event_triggers_disabled",
                        "role_superuser",
                        "role_create_db",
                        "role_create_role",
                        "role_replication",
                        "role_bypass_rls",
                        "role_inherit",
                        "role_can_login",
                        "role_memberships",
                        "role_incoming_memberships",
                        "role_settings",
                        "role_owned_objects",
                        "role_direct_acl",
                        "role_default_acl",
                        "event_triggers",
                        "role_effective_persistent_write",
                        "ledger",
                        "ledger_sha256",
                        "ledger_relation",
                        "ledger_analysis",
                        "legacy_relation_present",
                        "generation_schema",
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
        "ctime_ns": 5,
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
            "online_admin_role": None,
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
                    "server_startup",
                    "event_triggers_disabled",
                    "role_superuser",
                    "role_create_db",
                    "role_create_role",
                    "role_replication",
                    "role_bypass_rls",
                    "role_inherit",
                    "role_can_login",
                    "role_memberships",
                    "role_incoming_memberships",
                    "role_settings",
                    "role_owned_objects",
                    "role_direct_acl",
                    "role_default_acl",
                    "event_triggers",
                    "role_effective_persistent_write",
                    "ledger",
                    "ledger_sha256",
                    "ledger_relation",
                    "ledger_analysis",
                    "legacy_relation_present",
                    "generation_schema",
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
            "role_replication": record["role_replication"],
            "role_bypass_rls": record["role_bypass_rls"],
            "role_inherit": record["role_inherit"],
            "role_can_login": record["role_can_login"],
            "role_memberships": record["role_memberships"],
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
        "schema_version": 5,
        "inventory_complete": True,
        "writable_target": {"stack": "production", "database": "nexpoly"},
        "media_registry": {
            "schema_version": 5,
            "media_authority_rules_sha256": "sha256:" + "8" * 64,
            "runtime_registry_sha256": registry_digest,
            "reviewed_content_inventory_sha256": "sha256:" + "9" * 64,
            "audit_images": {
                str(major): {
                    "digest_ref": image,
                    "image_id": "sha256:"
                    + ("5" if major == 16 else str(major % 10)) * 64,
                }
                for major, image in MEDIA.POSTGRES_AUDIT_IMAGES.items()
            },
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
            "scanned_bind_sources": [],
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
    def test_site_startup_projection_distinguishes_online_and_isolated(
        self,
    ) -> None:
        data_directory = "/var/lib/postgresql/data"
        isolated = audited_startup_fields(data_directory)
        online = audited_startup_fields(
            data_directory,
            online=True,
        )
        self.assertEqual(
            CONTRACTS._external_server_startup_v3(
                isolated,
                data_directory=data_directory,
                online=False,
            ),
            isolated,
        )
        self.assertEqual(
            CONTRACTS._external_server_startup_v3(
                online,
                data_directory=data_directory,
                online=True,
            ),
            online,
        )
        for baseline, online_mode, field, value in (
            (isolated, False, "archive_command", ""),
            (isolated, False, "archive_command", "/bin/false"),
            (isolated, False, "restore_command", ""),
            (isolated, False, "restore_command", "(disabled)"),
            (online, True, "archive_command", "(disabled)"),
            (online, True, "restore_command", "/bin/false"),
        ):
            with self.subTest(
                online=online_mode,
                field=field,
                value=value,
            ):
                changed = copy.deepcopy(baseline)
                changed[field] = value
                with self.assertRaisesRegex(
                    CONTRACTS.SiteHelperContractError,
                    "startup configuration is unsafe",
                ):
                    CONTRACTS._external_server_startup_v3(
                        changed,
                        data_directory=data_directory,
                        online=online_mode,
                    )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="postgres-media-builder-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.evidence = self.root / "evidence"
        private_directory(self.evidence)

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
            descriptors=descriptors,
            required_online_databases=(
                {"stack": "nexpoly_dev", "media_id": identifiers[1]},
                {
                    "stack": "nexpoly_md_health_opt",
                    "media_id": identifiers[2],
                },
            ),
            boundary={"schema_version": 1, "fixture": True},
            audit_images=tuple(
                sorted(MEDIA.POSTGRES_AUDIT_IMAGES.items())
            ),
            audit_image_ids=tuple(
                (
                    major,
                    "sha256:" + ("5" if major == 16 else str(major % 10)) * 64,
                )
                for major in sorted(MEDIA.POSTGRES_AUDIT_IMAGES)
            ),
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
            scanned_bind_sources=(),
            scanned_container_ids=(CONTAINER_A, CONTAINER_B, CONTAINER_C),
        )
        reviewed_document = {
            "schema_version": 1,
            "media_authority_rules_sha256": (
                registry.authority_rules_sha256
            ),
            "discovery_state_sha256": MEDIA._discovery_state_sha256(
                discovery
            ),
            "media": [],
        }
        reviewed_payload = (
            MEDIA.canonical_json_bytes(reviewed_document) + b"\n"
        )
        reviewed_file = self.root / "reviewed-content.json"
        private_file(reviewed_file, reviewed_payload)
        registry = replace(
            registry,
            reviewed_content_inventory_sha256=MEDIA.sha256_bytes(
                reviewed_payload
            ),
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

        def live_audit(
            _runner,
            current,
            _source,
            *,
            trusted_image_id,
            expected_role_contracts,
        ):
            del trusted_image_id, expected_role_contracts
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
            value["server_startup"] = audited_startup_fields(
                "/var/lib/postgresql/data",
                online=True,
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

        def source_system_identifier(
            _runner,
            source,
            *,
            trusted_image_id,
        ):
            del trusted_image_id
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
            mock.patch.object(MEDIA, "_revalidate_docker_epoch"),
            mock.patch.object(MEDIA, "_revalidate_backup_epoch"),
            mock.patch.dict(
                os.environ,
                {
                    MEDIA.AUDIT_ROLE_SQL_DIGEST_ENV: (
                        "sha256:" + "8" * 64
                    )
                },
            ),
        ):
            envelope = MEDIA.build_evidence(
                registry,
                discovery,
                runner=MEDIA.CommandRunner(),
                evidence_root=self.evidence,
                operation=PassthroughScratchOperation(
                    MEDIA.CommandRunner(),
                    self.evidence,
                ),
                now=lambda: "2026-07-17T12:00:00Z",
                reviewed_content_file=reviewed_file,
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
            "mixes auditor implementations|mixes audit runtimes",
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

    def test_schema_v5_rejects_postgresql_record_only_evidence(self) -> None:
        ledger = [
            {"version": version, "checksum": checksum}
            for version, checksum in MEDIA.CANONICAL_MIGRATION_LEDGER[:8]
        ]
        envelope = external_inventory_fixture(
            dev_ledger=ledger,
            health_ledger=ledger,
            registry_digest="sha256:" + "c" * 64,
            dev_user="dev_auditor",
            health_user="health_auditor",
        )
        envelope["media"].append(
            {"record_type": "adjacent-record-only"}
        )
        with self.assertRaisesRegex(
            CONTRACTS.SiteHelperContractError,
            "requires a full logical audit",
        ):
            CONTRACTS.validate_external_database_audit(
                envelope,
                expected_users={
                    "nexpoly_dev": "dev_auditor",
                    "nexpoly_md_health_opt": "health_auditor",
                },
                expected_runtime_registry_digest=(
                    "sha256:" + "c" * 64
                ),
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
                "statement_timeout": "5min",
                "lock_timeout": "5s",
                "search_path": "pg_catalog",
                "row_security": True,
                **database_startup_fields(
                    "/var/lib/postgresql/data",
                    isolated=True,
                ),
                "role_superuser": True,
                "role_create_db": True,
                "role_create_role": True,
                "role_replication": True,
                "role_bypass_rls": True,
                "role_inherit": True,
                "role_can_login": True,
                **role_security_fields("nexpoly", superuser=True),
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
                "generation_schema": None,
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
        )
        registry = MEDIA.Registry(
            payload=b"fixture",
            digest=MEDIA.sha256_bytes(b"fixture"),
            audit_image=IMAGE,
            auditor_sha256=MEDIA._auditor_digest(),
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
        for setting in (
            "archive_command=/bin/false",
            "restore_command=/bin/false",
            "shared_preload_libraries=",
            "session_preload_libraries=",
            "local_preload_libraries=",
        ):
            self.assertEqual(run_command.count(setting), 1)
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

    def test_isolated_startup_arguments_pin_command_execution_hooks(
        self,
    ) -> None:
        for major in MEDIA.SUPPORTED_POSTGRES_AUDIT_MAJORS:
            with self.subTest(postgres_major=major):
                arguments = MEDIA._isolated_postgres_arguments(
                    postgres_major=major,
                    pgdata="/var/lib/postgresql/data",
                )
                for setting in (
                    "archive_command=/bin/false",
                    "restore_command=/bin/false",
                    "shared_preload_libraries=",
                    "session_preload_libraries=",
                    "local_preload_libraries=",
                ):
                    self.assertEqual(arguments.count(setting), 1)
                self.assertEqual(
                    arguments.count("event_triggers=false"),
                    1 if major >= 17 else 0,
                )

    def test_real_resource_cleanup_rejects_ambiguous_inspect_errors(
        self,
    ) -> None:
        class InspectRunner:
            def __init__(self, *, ambiguous: bool) -> None:
                self.ambiguous = ambiguous
                self.calls: list[list[str]] = []

            def run(self, arguments, **_kwargs):
                values = list(arguments)
                self.calls.append(values)
                resource = values[1]
                if self.ambiguous:
                    return subprocess.CompletedProcess(
                        values,
                        125,
                        stdout=b"",
                        stderr=b"docker daemon unavailable",
                    )
                return subprocess.CompletedProcess(
                    values,
                    1,
                    stdout=b"",
                    stderr=(
                        b"Error: No such container"
                        if resource == "container"
                        else b"Error: No such volume"
                    ),
                )

        ambiguous = InspectRunner(ambiguous=True)
        with self.assertRaises(ExceptionGroup):
            RealDockerPostgresIntegrationTests._cleanup_labeled_container_and_volume(
                ambiguous,  # type: ignore[arg-type]
                container_name="owned-container",
                volume_name="owned-volume",
                label="owned-fixture",
            )
        self.assertEqual(
            [call[1] for call in ambiguous.calls],
            ["container", "volume"],
        )

        absent = InspectRunner(ambiguous=False)
        RealDockerPostgresIntegrationTests._cleanup_labeled_container_and_volume(
            absent,  # type: ignore[arg-type]
            container_name="absent-container",
            volume_name="absent-volume",
            label="absent-fixture",
        )
        self.assertEqual(
            [call[1] for call in absent.calls],
            ["container", "volume"],
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
    @staticmethod
    def _cleanup_labeled_container_and_volume(
        runner: MEDIA.CommandRunner,
        *,
        container_name: str,
        volume_name: str,
        label: str,
    ) -> None:
        failures: list[Exception] = []

        def is_absent(
            completed: subprocess.CompletedProcess[bytes],
            *,
            resource: str,
        ) -> bool:
            error = completed.stderr.decode(
                "utf-8",
                "replace",
            ).lower()
            if completed.returncode != 1:
                return False
            if resource == "container":
                return (
                    "no such container" in error
                    or "no such object" in error
                )
            return "no such volume" in error

        container: subprocess.CompletedProcess[bytes] | None = None
        try:
            container = runner.run(
                [
                    MEDIA.DOCKER,
                    "container",
                    "inspect",
                    "--",
                    container_name,
                ],
                check=False,
            )
        except Exception as exc:
            failures.append(exc)
        if container is not None and container.returncode == 0:
            try:
                records = json.loads(container.stdout)
                if (
                    not isinstance(records, list)
                    or len(records) != 1
                    or records[0]
                    .get("Config", {})
                    .get("Labels", {})
                    .get("io.nexpoly.test")
                    != label
                ):
                    raise AssertionError(
                        "ephemeral PostgreSQL container ownership differs"
                    )
                container_id = str(records[0]["Id"])
                runner.run(
                    [
                        MEDIA.DOCKER,
                        "container",
                        "rm",
                        "-f",
                        "--",
                        container_id,
                    ],
                    check=False,
                )
                after = runner.run(
                    [
                        MEDIA.DOCKER,
                        "container",
                        "inspect",
                        "--",
                        container_id,
                    ],
                    check=False,
                )
                if after.returncode == 0:
                    raise AssertionError(
                        "ephemeral PostgreSQL container cleanup failed"
                    )
                if not is_absent(
                    after,
                    resource="container",
                ):
                    raise AssertionError(
                        "cannot prove ephemeral container is absent"
                    )
            except Exception as exc:
                failures.append(exc)
        elif container is not None and not is_absent(
            container,
            resource="container",
        ):
            failures.append(
                AssertionError(
                    "cannot inspect ephemeral PostgreSQL container"
                )
            )

        volume: subprocess.CompletedProcess[bytes] | None = None
        try:
            volume = runner.run(
                [
                    MEDIA.DOCKER,
                    "volume",
                    "inspect",
                    "--",
                    volume_name,
                ],
                check=False,
            )
        except Exception as exc:
            failures.append(exc)
        if volume is not None and volume.returncode == 0:
            try:
                records = json.loads(volume.stdout)
                if (
                    not isinstance(records, list)
                    or len(records) != 1
                    or records[0]
                    .get("Labels", {})
                    .get("io.nexpoly.test")
                    != label
                ):
                    raise AssertionError(
                        "ephemeral PostgreSQL volume ownership differs"
                    )
                runner.run(
                    [
                        MEDIA.DOCKER,
                        "volume",
                        "rm",
                        "-f",
                        "--",
                        volume_name,
                    ],
                    check=False,
                )
                after = runner.run(
                    [
                        MEDIA.DOCKER,
                        "volume",
                        "inspect",
                        "--",
                        volume_name,
                    ],
                    check=False,
                )
                if after.returncode == 0:
                    raise AssertionError(
                        "ephemeral PostgreSQL volume cleanup failed"
                    )
                if not is_absent(
                    after,
                    resource="volume",
                ):
                    raise AssertionError(
                        "cannot prove ephemeral volume is absent"
                    )
            except Exception as exc:
                failures.append(exc)
        elif volume is not None and not is_absent(
            volume,
            resource="volume",
        ):
            failures.append(
                AssertionError(
                    "cannot inspect ephemeral PostgreSQL volume"
                )
            )
        if failures:
            raise ExceptionGroup(
                "ephemeral PostgreSQL resource cleanup failed",
                failures,
            )

    def postgres_major(self) -> int:
        try:
            major = int(os.environ["NEXPOLY_TEST_POSTGRES_MAJOR"])
        except (KeyError, ValueError) as exc:
            self.fail(
                "NEXPOLY_TEST_POSTGRES_MAJOR must select one pinned major"
            )
            raise AssertionError from exc
        if major not in MEDIA.POSTGRES_AUDIT_IMAGES:
            self.fail("integration PostgreSQL major is unsupported")
        return major

    def pinned_image(self) -> str:
        major = self.postgres_major()
        image = os.environ["NEXPOLY_TEST_POSTGRES_IMAGE"]
        if MEDIA.IMAGE_RE.fullmatch(image) is None:
            self.fail("NEXPOLY_TEST_POSTGRES_IMAGE must be a full image digest")
        if image != MEDIA.POSTGRES_AUDIT_IMAGES[major]:
            self.fail(
                "integration image must equal the source-pinned major digest"
            )
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
        version = runner.run(
            [
                MEDIA.DOCKER,
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "postgres",
                image,
                "--version",
            ]
        ).stdout.decode("utf-8", "strict")
        self.assertRegex(version, rf"\b{major}(?:\.[0-9]+)?\b")
        return image

    def test_pg18_role_contract_disables_login_event_triggers(
        self,
    ) -> None:
        if self.postgres_major() != 18:
            self.skipTest(
                "login event-trigger provisioning regression is PG18-only"
            )
        image = self.pinned_image()
        runner = MEDIA.CommandRunner()
        volume = MEDIA._temp_name("integration-pg18-role")
        name = MEDIA._temp_name("integration-pg18-role-pg")
        label = "pg18-role-contract-integration"
        container_id: str | None = None
        try:
            runner.run(
                [
                    MEDIA.DOCKER,
                    "volume",
                    "create",
                    "--label",
                    f"io.nexpoly.test={label}",
                    "--",
                    volume,
                ]
            )
            completed = runner.run(
                [
                    MEDIA.DOCKER,
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--label",
                    f"io.nexpoly.test={label}",
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
                        f"type=volume,src={volume},"
                        "dst=/var/lib/postgresql"
                    ),
                    "--env",
                    "POSTGRES_HOST_AUTH_METHOD=trust",
                    "--env",
                    "POSTGRES_USER=postgres",
                    "--env",
                    "POSTGRES_DB=postgres",
                    image,
                ],
                timeout=120,
            )
            container_id = completed.stdout.decode(
                "ascii",
                "strict",
            ).strip()
            MEDIA._wait_for_postgres(
                runner,
                container_id,
                database="postgres",
                user="postgres",
            )
            inventory = MEDIA._container_database_inventory(
                runner,
                container_id,
                postgres_major=18,
            )
            self.assertEqual(
                [record["name"] for record in inventory],
                ["postgres"],
            )
            database = inventory[0]
            audit_image_id = MEDIA._local_audit_image_id(
                runner,
                image,
            )
            MEDIA._run_trusted_psql(
                runner,
                container_id=container_id,
                postgres_major=18,
                pgoptions=MEDIA._psql_provision_pgoptions(18),
                arguments=[
                    "-v",
                    "audit_role=nexpoly_pg18_auditor",
                    "-v",
                    "audit_database=postgres",
                    "-v",
                    f"expected_database_oid={database['oid']}",
                    "-v",
                    f"expected_database_owner={database['owner']}",
                    "-v",
                    "expected_session_user=postgres",
                    "-v",
                    "expected_event_triggers_disabled=true",
                    "-v",
                    "role_contract_sha256=sha256:" + "a" * 64,
                    "-U",
                    "postgres",
                    "-d",
                    "postgres",
                ],
                input_bytes=(
                    ROOT
                    / "ops/config/postgres-media-audit-role.sql.example"
                ).read_bytes(),
                timeout=600,
                expected_image_id=audit_image_id,
            )
            role = runner.run(
                [
                    MEDIA.DOCKER,
                    "exec",
                    "--user",
                    "postgres",
                    container_id,
                    "psql",
                    "-X",
                    "-A",
                    "-t",
                    "-U",
                    "postgres",
                    "-d",
                    "postgres",
                    "-c",
                    (
                        "SELECT rolname FROM pg_catalog.pg_roles "
                        "WHERE rolname = 'nexpoly_pg18_auditor';"
                    ),
                ],
                timeout=120,
            ).stdout.decode("ascii", "strict").strip()
            self.assertEqual(role, "nexpoly_pg18_auditor")
        finally:
            self._cleanup_labeled_container_and_volume(
                runner,
                container_name=name,
                volume_name=volume,
                label=label,
            )

    def test_mutable_schema_v6_helper_runs_against_real_postgres(
        self,
    ) -> None:
        if self.postgres_major() != 16:
            self.skipTest("mutable schema-v6 integration is fixed to PG16")
        if (
            os.environ.get("NEXPOLY_RUN_MUTABLE_HELPER_INTEGRATION")
            != "1"
        ):
            self.skipTest(
                "mutable helper integration requires its explicit CI gate"
            )
        image = self.pinned_image()
        runner = MEDIA.CommandRunner()
        volume = MEDIA._temp_name("integration-mutable-v6")
        name = MEDIA._temp_name("integration-mutable-v6-pg")
        label = "mutable-schema-v6-integration"
        password = "mutable-v6-admin-secret"
        audit_password = "mutable-v6-audit-secret"
        production_host_port = 55432
        container_id: str | None = None
        try:
            runner.run(
                [
                    MEDIA.DOCKER,
                    "volume",
                    "create",
                    "--label",
                    f"io.nexpoly.test={label}",
                    "--",
                    volume,
                ]
            )
            completed = runner.run(
                [
                    MEDIA.DOCKER,
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--label",
                    f"io.nexpoly.test={label}",
                    "--publish",
                    "127.0.0.1::5432",
                    "--read-only",
                    "--tmpfs",
                    (
                        "/var/run/postgresql:rw,noexec,nosuid,size=16m,"
                        "uid=70,gid=70,mode=0700"
                    ),
                    "--mount",
                    (
                        f"type=volume,src={volume},"
                        "dst=/var/lib/postgresql/data"
                    ),
                    "--env",
                    f"POSTGRES_PASSWORD={password}",
                    "--env",
                    "POSTGRES_USER=postgres",
                    "--env",
                    "POSTGRES_DB=nexpoly",
                    image,
                ],
                timeout=120,
            )
            container_id = completed.stdout.decode(
                "ascii",
                "strict",
            ).strip()
            MEDIA._wait_for_postgres(
                runner,
                container_id,
                database="nexpoly",
                user="postgres",
            )
            container_record = json.loads(
                runner.run(
                    [
                        MEDIA.DOCKER,
                        "container",
                        "inspect",
                        "--",
                        container_id,
                    ]
                ).stdout
            )
            if (
                not isinstance(container_record, list)
                or len(container_record) != 1
            ):
                self.fail(
                    "mutable schema-v6 integration container is ambiguous"
                )
            bindings = (
                container_record[0]
                .get("NetworkSettings", {})
                .get("Ports", {})
                .get("5432/tcp")
            )
            if (
                not isinstance(bindings, list)
                or len(bindings) != 1
                or bindings[0].get("HostIp") != "127.0.0.1"
                or re.fullmatch(
                    r"[1-9][0-9]{0,4}",
                    str(bindings[0].get("HostPort")),
                )
                is None
            ):
                self.fail(
                    "mutable schema-v6 integration port is not isolated"
                )
            host_port = int(bindings[0]["HostPort"])
            if host_port == production_host_port:
                self.fail(
                    "Docker selected the fixed production PostgreSQL port"
                )

            def admin_sql(payload: bytes) -> None:
                runner.run(
                    [
                        MEDIA.DOCKER,
                        "exec",
                        "-i",
                        container_id,
                        "psql",
                        "-X",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-U",
                        "postgres",
                        "-d",
                        "nexpoly",
                    ],
                    input_bytes=payload,
                    timeout=120,
                )

            migration_root = ROOT / "backend/migrations/postgres"
            migration_manifest = json.loads(
                (migration_root / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            migrations = {
                record["version"]: record
                for record in migration_manifest["migrations"]
            }
            through_0011 = [
                record
                for record in migration_manifest["migrations"]
                if record["version"] <= "0011_monomer_md_demo_steps"
            ]
            for record in through_0011:
                admin_sql(
                    (
                        migration_root / f"{record['version']}.sql"
                    ).read_bytes()
                )
                admin_sql(
                    (
                        "INSERT INTO governance.schema_migrations "
                        "(version, checksum) VALUES "
                        f"('{record['version']}',"
                        f"'{record['checksum']}');"
                    ).encode("ascii")
                )
            admin_sql(
                (
                    ROOT
                    / "ops/config/mutable-data-audit-role.sql.example"
                ).read_bytes()
            )
            admin_sql(
                (
                    "ALTER ROLE nexpoly_mutable_audit PASSWORD "
                    f"'{audit_password}';"
                ).encode("ascii")
            )

            system_identifier = runner.run(
                [
                    MEDIA.DOCKER,
                    "exec",
                    container_id,
                    "psql",
                    "-X",
                    "-A",
                    "-t",
                    "-U",
                    "postgres",
                    "-d",
                    "nexpoly",
                    "-c",
                    (
                        "SELECT system_identifier::text "
                        "FROM pg_catalog.pg_control_system();"
                    ),
                ],
                timeout=120,
            ).stdout.decode("ascii", "strict").strip()
            image_id = runner.run(
                [
                    MEDIA.DOCKER,
                    "container",
                    "inspect",
                    "--format",
                    "{{.Image}}",
                    "--",
                    container_id,
                ]
            ).stdout.decode("ascii", "strict").strip()
            volume_record = json.loads(
                runner.run(
                    [
                        MEDIA.DOCKER,
                        "volume",
                        "inspect",
                        "--",
                        volume,
                    ]
                ).stdout
            )[0]
            runtime_identity = {
                "container_id": container_id,
                "image_id": image_id,
                "configured_image": image,
                "data_volume": {
                    "type": "volume",
                    "name": volume,
                    "source": volume_record["Mountpoint"],
                    "destination": "/var/lib/postgresql/data",
                    "driver": volume_record["Driver"],
                    "read_write": True,
                },
                "host_endpoint": {
                    "host": "127.0.0.1",
                    "port": host_port,
                    "container_port": 5432,
                    "protocol": "tcp",
                },
                "system_identifier": system_identifier,
            }

            with tempfile.TemporaryDirectory(
                prefix="mutable-v6-real-"
            ) as raw:
                root = Path(raw)
                os.chmod(root, 0o700)
                runtime = root / "runtime"
                config = runtime / "config"
                audit = runtime / "audit/mutable-data"
                config.mkdir(parents=True, mode=0o700)
                audit.mkdir(parents=True, mode=0o700)
                service = config / "mutable-data-audit.pg_service.conf"
                service.write_text(
                    "[nexpoly-mutable-audit]\n"
                    "host=127.0.0.1\n"
                    f"port={host_port}\n"
                    "dbname=nexpoly\n"
                    "user=nexpoly_mutable_audit\n"
                    "sslmode=disable\n"
                    f"passfile={config / 'mutable-data-audit.pgpass'}\n",
                    encoding="utf-8",
                )
                os.chmod(service, 0o600)
                pgpass = config / "mutable-data-audit.pgpass"
                pgpass.write_text(
                    (
                        f"127.0.0.1:{host_port}:nexpoly:"
                        f"nexpoly_mutable_audit:{audit_password}\n"
                    ),
                    encoding="utf-8",
                )
                os.chmod(pgpass, 0o600)
                helper = config / "deployment-mutable-data-audit"
                helper_template = (
                    ROOT
                    / "ops/config/deployment-mutable-data-audit.example"
                ).read_text(encoding="utf-8")
                self.assertEqual(
                    helper_template.count('    "port": 55432,\n'),
                    1,
                )
                helper.write_text(
                    helper_template
                    .replace(
                        (
                            'readonly runtime_root="'
                            '/data/lzq/gith/nexpoly-runtime"'
                        ),
                        f'readonly runtime_root="{runtime}"',
                        1,
                    )
                    .replace(
                        '    "port": 55432,\n',
                        f'    "port": {host_port},\n',
                        1,
                    ),
                    encoding="utf-8",
                )
                os.chmod(helper, 0o700)

                def capture(operation_id: str) -> dict[str, object]:
                    result = subprocess.run(
                        [str(helper)],
                        env={
                            "PATH": "/usr/bin:/bin",
                            "NEXPOLY_MUTABLE_AUDIT_OPERATION_ID": (
                                operation_id
                            ),
                            "NEXPOLY_MUTABLE_AUDIT_RUNTIME_JSON": (
                                json.dumps(
                                    runtime_identity,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                )
                            ),
                        },
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    self.assertEqual(
                        result.returncode,
                        0,
                        result.stderr,
                    )
                    return CONTRACTS._validate_mutable_data_audit(
                        json.loads(result.stdout),
                        expected_connection_port=host_port,
                    )

                before = capture("mutable-v6-pre-0012")
                contract = migrations["0012_drop_polytao_jobs"]
                admin_sql(
                    (
                        migration_root / f"{contract['version']}.sql"
                    ).read_bytes()
                )
                admin_sql(
                    (
                        "INSERT INTO governance.schema_migrations "
                        "(version, checksum) VALUES "
                        f"('{contract['version']}',"
                        f"'{contract['checksum']}');"
                    ).encode("ascii")
                )
                after = capture("mutable-v6-post-0012")

            self.assertEqual(before["schema_version"], 6)
            self.assertEqual(after["schema_version"], 6)
            self.assertEqual(
                before["business_tables"],
                after["business_tables"],
            )
            self.assertEqual(
                before["static_tables"],
                after["static_tables"],
            )
            self.assertEqual(
                before["governed_controls"],
                after["governed_controls"],
            )
            self.assertEqual(
                before["bridge_projection"],
                after["bridge_projection"],
            )
            self.assertEqual(
                before["sequences"],
                after["sequences"],
            )
            self.assertEqual(
                before["migration_exception"]["state"],
                "present",
            )
            self.assertIsNotNone(
                before["migration_exception_archive_evidence"]
            )
            self.assertEqual(
                after["migration_exception"]["state"],
                "absent",
            )
            self.assertIsNone(
                after["migration_exception_archive_evidence"]
            )
        finally:
            self._cleanup_labeled_container_and_volume(
                runner,
                container_name=name,
                volume_name=volume,
                label=label,
            )

    def test_rotated_password_uses_only_the_sealed_credential_fd(
        self,
    ) -> None:
        if self.postgres_major() != 16:
            self.skipTest("sealed credential regression is fixed to PG16")
        image = self.pinned_image()

        class RedactingRunner(MEDIA.CommandRunner):
            def __init__(self) -> None:
                self.commands: list[list[str]] = []
                self.environments: list[dict[str, str]] = []

            def run(self, arguments, **kwargs):
                self.commands.append(list(arguments))
                self.environments.append(dict(kwargs.get("env") or {}))
                return super().run(arguments, **kwargs)

        runner = RedactingRunner()
        volume = MEDIA._temp_name("integration-sealed-credential")
        name = MEDIA._temp_name("integration-sealed-credential-pg")
        admin = "sealed_admin"
        database = "sealed_database"
        stale_secret = "stale-container-secret"
        current_secret = "current-rotated-secret"
        wrong_secret = "wrong-envelope-secret"
        container_id: str | None = None
        runner.run([MEDIA.DOCKER, "volume", "create", "--", volume])
        try:
            completed = runner.run(
                [
                    MEDIA.DOCKER,
                    "run",
                    "-d",
                    "--name",
                    name,
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
                        f"type=volume,src={volume},"
                        "dst=/var/lib/postgresql/data"
                    ),
                    "--env",
                    f"POSTGRES_PASSWORD={stale_secret}",
                    "--env",
                    f"POSTGRES_USER={admin}",
                    "--env",
                    f"POSTGRES_DB={database}",
                    image,
                ],
                timeout=120,
            )
            container_id = completed.stdout.decode(
                "ascii",
                "strict",
            ).strip()
            MEDIA._wait_for_postgres(
                runner,
                container_id,
                database=database,
                user=admin,
            )
            runner.run(
                [
                    MEDIA.DOCKER,
                    "exec",
                    "--user",
                    "postgres",
                    container_id,
                    "/bin/sh",
                    "-ceu",
                    (
                        "umask 077; "
                        "printf '%s\\n' "
                        "'local all all trust' "
                        "'host all all 127.0.0.1/32 scram-sha-256' "
                        "'host all all ::1/128 scram-sha-256' "
                        "> \"$PGDATA/pg_hba.conf\""
                    ),
                ],
                timeout=120,
            )
            runner.run(
                [
                    MEDIA.DOCKER,
                    "exec",
                    "--user",
                    "postgres",
                    "-i",
                    container_id,
                    "psql",
                    "-X",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-h",
                    "/var/run/postgresql",
                    "-U",
                    admin,
                    "-d",
                    database,
                ],
                input_bytes=(
                    f"ALTER ROLE {admin} PASSWORD "
                    f"'{current_secret}';"
                    "SELECT pg_catalog.pg_reload_conf();"
                ).encode("utf-8"),
                timeout=120,
            )
            system_identifier = runner.run(
                [
                    MEDIA.DOCKER,
                    "exec",
                    "--user",
                    "postgres",
                    container_id,
                    "psql",
                    "-X",
                    "-A",
                    "-t",
                    "-h",
                    "/var/run/postgresql",
                    "-U",
                    admin,
                    "-d",
                    database,
                    "-c",
                    (
                        "SELECT system_identifier::text "
                        "FROM pg_catalog.pg_control_system();"
                    ),
                ],
                timeout=120,
            ).stdout.decode("ascii", "strict").strip()
            runner.run(
                [
                    MEDIA.DOCKER,
                    "exec",
                    "--user",
                    "postgres",
                    container_id,
                    "psql",
                    "-X",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-h",
                    "/var/run/postgresql",
                    "-U",
                    admin,
                    "-d",
                    database,
                    "-c",
                    "ALTER DATABASE postgres WITH ALLOW_CONNECTIONS false;",
                ],
                timeout=120,
            )
            audit_image_id = MEDIA._local_audit_image_id(
                runner,
                image,
            )
            audit_command_start = len(runner.commands)
            with inherited_database_credentials(
                database_credentials_document(
                    container_id=container_id,
                    system_identifier=system_identifier,
                    online_admin_role=admin,
                    password=current_secret,
                )
            ):
                selected = MEDIA._run_trusted_psql(
                    runner,
                    container_id=container_id,
                    postgres_major=16,
                    pgoptions=MEDIA.PSQL_AUDIT_PGOPTIONS,
                    arguments=[
                        "-U",
                        admin,
                        "-d",
                        database,
                        "-c",
                        "SELECT 42;",
                    ],
                    timeout=120,
                    expected_image_id=audit_image_id,
                )
            self.assertEqual(
                selected.stdout.decode("ascii", "strict").strip(),
                "42",
            )
            audit_commands = runner.commands[audit_command_start:]
            audit_environments = runner.environments[
                audit_command_start:
            ]
            for secret in (stale_secret, current_secret, wrong_secret):
                self.assertTrue(
                    all(
                        secret not in "\0".join(command)
                        for command in audit_commands
                    )
                )
                self.assertTrue(
                    all(
                        secret
                        not in "\0".join(
                            f"{key}={value}"
                            for key, value in environment.items()
                        )
                        for environment in audit_environments
                    )
                )

            with inherited_database_credentials(
                database_credentials_document(
                    container_id=container_id,
                    system_identifier=system_identifier,
                    online_admin_role=admin,
                    password=wrong_secret,
                )
            ):
                with self.assertRaises(
                    MEDIA.MediaEvidenceError
                ) as raised:
                    MEDIA._run_trusted_psql(
                        runner,
                        container_id=container_id,
                        postgres_major=16,
                        pgoptions=MEDIA.PSQL_AUDIT_PGOPTIONS,
                        arguments=[
                            "-U",
                            admin,
                            "-d",
                            database,
                            "-c",
                            "SELECT 99;",
                        ],
                        timeout=120,
                        expected_image_id=audit_image_id,
                    )
            rendered_error = str(raised.exception)
            self.assertNotIn(stale_secret, rendered_error)
            self.assertNotIn(current_secret, rendered_error)
            self.assertNotIn(wrong_secret, rendered_error)
        finally:
            if container_id is not None:
                runner.run(
                    [
                        MEDIA.DOCKER,
                        "container",
                        "rm",
                        "-f",
                        "--",
                        container_id,
                    ],
                    timeout=120,
                    check=False,
                )
            runner.run(
                [MEDIA.DOCKER, "volume", "rm", "-f", "--", volume],
                timeout=120,
                check=False,
            )

    def test_live_custom_admin_uses_trusted_loopback_client(
        self,
    ) -> None:
        if self.postgres_major() != 16:
            self.skipTest("live custom-admin regression is fixed to PG16")
        image = self.pinned_image()

        class RecordingRunner(MEDIA.CommandRunner):
            def __init__(self) -> None:
                self.commands: list[list[str]] = []

            def run(self, arguments, **kwargs):
                self.commands.append(list(arguments))
                return super().run(arguments, **kwargs)

        runner = RecordingRunner()
        volume = MEDIA._temp_name("integration-custom-admin")
        container_name = MEDIA._temp_name("integration-custom-admin-pg")
        admin = "Poly Admin"
        database = "Nexpoly Audit DB"
        container_id: str | None = None
        runner.run([MEDIA.DOCKER, "volume", "create", "--", volume])
        try:
            completed = runner.run(
                [
                    MEDIA.DOCKER,
                    "run",
                    "-d",
                    "--name",
                    container_name,
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
                        f"type=volume,src={volume},"
                        "dst=/var/lib/postgresql/data"
                    ),
                    "--env",
                    "POSTGRES_HOST_AUTH_METHOD=trust",
                    "--env",
                    f"POSTGRES_USER={admin}",
                    "--env",
                    f"POSTGRES_DB={database}",
                    image,
                ],
                timeout=120,
            )
            container_id = completed.stdout.decode(
                "ascii", "strict"
            ).strip()
            MEDIA._wait_for_postgres(
                runner,
                container_id,
                database=database,
                user=admin,
            )

            def admin_sql(
                statement: str,
                *,
                target_database: str = database,
            ) -> None:
                runner.run(
                    [
                        MEDIA.DOCKER,
                        "exec",
                        "--user",
                        "postgres",
                        "-i",
                        container_id,
                        "psql",
                        "-X",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-h",
                        "/var/run/postgresql",
                        "-U",
                        admin,
                        "-d",
                        target_database,
                    ],
                    input_bytes=statement.encode("utf-8"),
                )

            def normalized_dump(arguments: list[str]) -> bytes:
                completed_dump = runner.run(
                    [
                        MEDIA.DOCKER,
                        "exec",
                        "--user",
                        "postgres",
                        container_id,
                        *arguments,
                    ],
                    timeout=600,
                )
                # Newer pg_dump clients add random psql restriction tokens.
                # They are transport guards rather than cluster state.
                return b"\n".join(
                    line
                    for line in completed_dump.stdout.splitlines()
                    if not line.startswith(
                        (b"\\restrict ", b"\\unrestrict ")
                    )
                )

            def cluster_contract_state() -> tuple[str, str]:
                roles = normalized_dump(
                    [
                        "pg_dumpall",
                        "--roles-only",
                        "-h",
                        "/var/run/postgresql",
                        "-U",
                        admin,
                    ]
                )
                schema = normalized_dump(
                    [
                        "pg_dump",
                        "--schema-only",
                        "-h",
                        "/var/run/postgresql",
                        "-U",
                        admin,
                        "-d",
                        database,
                    ]
                )
                return (
                    MEDIA.sha256_bytes(roles),
                    MEDIA.sha256_bytes(schema),
                )

            version, checksum = MEDIA.CANONICAL_MIGRATION_LEDGER[0]
            remaining_ledger = ", ".join(
                f"('{migration_version}', '{migration_checksum}')"
                for migration_version, migration_checksum
                in MEDIA.CANONICAL_MIGRATION_LEDGER[1:11]
            )
            admin_sql(
                "CREATE SCHEMA governance AUTHORIZATION "
                f'"{admin}"; '
                "CREATE TABLE governance.schema_migrations ("
                "version text PRIMARY KEY, checksum text NOT NULL, "
                "applied_at timestamp with time zone NOT NULL "
                "DEFAULT now()); "
                "INSERT INTO governance.schema_migrations "
                "(version, checksum) VALUES "
                f"('{version}', '{checksum}');"
            )
            admin_sql(
                (
                    ROOT
                    / "backend/migrations/postgres/0007_polytao_jobs.sql"
                ).read_text(encoding="utf-8")
            )
            admin_sql(
                (
                    ROOT
                    / "backend/migrations/postgres/"
                    "0008_polytao_backend_runtime.sql"
                ).read_text(encoding="utf-8")
            )
            admin_sql(
                "INSERT INTO governance.schema_migrations "
                "(version, checksum) VALUES "
                + remaining_ledger
                + ";"
            )
            inspected = MEDIA._json_command(
                runner,
                [
                    MEDIA.DOCKER,
                    "container",
                    "inspect",
                    "--",
                    container_id,
                ],
            )[0]
            mount = next(
                value
                for value in inspected["Mounts"]
                if value.get("Type") == "volume"
                and value.get("Name") == volume
            )
            source = MEDIA.DiscoveredMedia(
                media_id=f"docker-volume:{volume}",
                kind="docker_volume",
                locator=volume,
                data_subpath=".",
                attached=(
                    MEDIA._attached_record(inspected, mount),
                ),
                signature="postgres",
                postgres_major=16,
            )
            authority = MEDIA.MediaAuthorityRules(
                payload=b"integration",
                digest=MEDIA.sha256_bytes(b"integration"),
                audit_image=image,
                auditor_sha256=MEDIA._auditor_digest(),
                descriptors=(),
                required_online_databases=(),
                policy=MEDIA.DiscoveryPolicy(),
                allow_unmatched_non_postgres=False,
                production_identity={},
                audit_images=tuple(
                    sorted(MEDIA.POSTGRES_AUDIT_IMAGES.items())
                ),
            )
            audit_image_id = MEDIA._local_audit_image_id(
                runner,
                image,
            )
            descriptor_value = MEDIA._live_runtime_descriptor(
                authority,
                source,
                primary_database=database,
                audit_role="nexpoly_custom_auditor",
                disposition="read-only-online",
                runner=runner,
                audit_image_id=audit_image_id,
            )
            role_contract = (
                ROOT / "ops/config/postgres-media-audit-role.sql.example"
            ).read_bytes()

            def run_role_contract(
                record: dict[str, object],
                *,
                role: str | None = None,
                contract_sha256: str = "sha256:" + "a" * 64,
            ) -> None:
                audit_role = role or str(record["audit_role"])
                MEDIA._run_trusted_psql(
                    runner,
                    container_id=container_id,
                    postgres_major=16,
                    pgoptions=MEDIA._psql_provision_pgoptions(16),
                    arguments=[
                        "-v",
                        f"audit_role={audit_role}",
                        "-v",
                        f"audit_database={record['name']}",
                        "-v",
                        f"expected_database_oid={record['oid']}",
                        "-v",
                        f"expected_database_owner={record['owner']}",
                        "-v",
                        f"expected_session_user={admin}",
                        "-v",
                        "expected_event_triggers_disabled=false",
                        "-v",
                        f"role_contract_sha256={contract_sha256}",
                        "-U",
                        admin,
                        "-d",
                        str(record["name"]),
                    ],
                    input_bytes=role_contract,
                    timeout=600,
                    expected_image_id=audit_image_id,
                )

            def drop_primary_audit_role(role: str) -> None:
                admin_sql(
                    f'REVOKE CONNECT ON DATABASE "{database}" '
                    f'FROM "{role}";'
                    "REVOKE EXECUTE ON FUNCTION "
                    "pg_catalog.pg_control_system() "
                    f'FROM "{role}";'
                    f'REVOKE SELECT ON governance.schema_migrations '
                    f'FROM "{role}";'
                    f'REVOKE USAGE ON SCHEMA governance FROM "{role}";'
                    f'DROP ROLE "{role}";'
                )

            primary_record = next(
                record
                for record in descriptor_value.databases
                if record["name"] == database
            )
            primary_audit_role = str(primary_record["audit_role"])
            collision_role = "nexpoly_collision_auditor"
            collision_parent = "nexpoly_collision_parent"
            admin_sql(
                f'CREATE ROLE "{collision_role}" '
                "LOGIN CREATEDB INHERIT;"
                f'COMMENT ON ROLE "{collision_role}" '
                "IS 'pre-existing business role';"
                f'CREATE ROLE "{collision_parent}" NOLOGIN;'
                f'GRANT "{collision_parent}" TO "{collision_role}";'
                f'ALTER ROLE "{collision_role}" '
                "SET statement_timeout = '17s';"
                f'CREATE SCHEMA collision_owned AUTHORIZATION '
                f'"{collision_role}";'
                f'GRANT CONNECT ON DATABASE "{database}" '
                f'TO "{collision_role}";'
                f'ALTER DEFAULT PRIVILEGES FOR ROLE "{admin}" '
                "IN SCHEMA public GRANT SELECT ON TABLES "
                f'TO "{collision_role}";'
            )
            collision_before = cluster_contract_state()
            with self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "NEXPOLY_PROVISION_REFUSED_ROLE_COLLISION",
            ):
                run_role_contract(
                    primary_record,
                    role=collision_role,
                    contract_sha256="sha256:" + "c" * 64,
                )
            self.assertEqual(
                cluster_contract_state(),
                collision_before,
            )
            admin_sql(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE "{admin}" '
                "IN SCHEMA public REVOKE SELECT ON TABLES "
                f'FROM "{collision_role}";'
                f'REVOKE CONNECT ON DATABASE "{database}" '
                f'FROM "{collision_role}";'
                "DROP SCHEMA collision_owned;"
                f'REVOKE "{collision_parent}" FROM "{collision_role}";'
                f'DROP ROLE "{collision_role}";'
                f'DROP ROLE "{collision_parent}";'
            )

            first_pass_state: tuple[str, str] | None = None
            for _pass in range(2):
                for record in descriptor_value.databases:
                    run_role_contract(record)
                current_state = cluster_contract_state()
                if first_pass_state is None:
                    first_pass_state = current_state
                else:
                    self.assertEqual(current_state, first_pass_state)
            bundle = MEDIA._run_live_audit(
                runner,
                descriptor_value,
                source,
                trusted_image_id=audit_image_id,
            )
            self.assertTrue(bundle["legacy_relation_present"])
            self.assertEqual(
                bundle["ledger_analysis"]["canonical_prefix_length"],
                11,
            )
            self.assertEqual(bundle["generation_schema"]["state"], "present")

            admin_sql(
                "CREATE TABLE public.nexpoly_cross_database_probe "
                "(value integer);"
                "GRANT SELECT ON public.nexpoly_cross_database_probe "
                f'TO "{primary_audit_role}";',
                target_database="postgres",
            )
            with self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "cross-database ACL",
            ):
                MEDIA._run_live_audit(
                    runner,
                    descriptor_value,
                    source,
                    trusted_image_id=audit_image_id,
                )
            admin_sql(
                "REVOKE SELECT ON public.nexpoly_cross_database_probe "
                f'FROM "{primary_audit_role}";'
                "DROP TABLE public.nexpoly_cross_database_probe;",
                target_database="postgres",
            )

            orphan_role = "nexpoly_orphan_auditor"
            admin_sql(
                f'CREATE ROLE "{orphan_role}" '
                "NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS NOINHERIT NOLOGIN;"
                f'COMMENT ON ROLE "{orphan_role}" IS '
                "'nexpoly-postgres-media-audit-role-v1:"
                "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
                "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee';"
                f'ALTER ROLE "{orphan_role}" '
                "SET default_transaction_read_only = on;"
                f'ALTER ROLE "{orphan_role}" '
                "SET statement_timeout = '5min';"
                f'ALTER ROLE "{orphan_role}" '
                "SET lock_timeout = '5s';"
            )
            with self.assertRaisesRegex(
                MEDIA.MediaEvidenceError,
                "orphan managed audit-role",
            ):
                MEDIA._run_live_audit(
                    runner,
                    descriptor_value,
                    source,
                    trusted_image_id=audit_image_id,
                )
            admin_sql(f'DROP ROLE "{orphan_role}";')

            migration_0012 = MEDIA.CANONICAL_MIGRATION_LEDGER[11]
            admin_sql(
                "BEGIN;\n"
                + (
                    ROOT
                    / "backend/migrations/postgres/"
                    "0012_drop_polytao_jobs.sql"
                ).read_text(encoding="utf-8")
                + "\nINSERT INTO governance.schema_migrations "
                "(version, checksum) VALUES "
                f"('{migration_0012[0]}', '{migration_0012[1]}');"
                "\nCOMMIT;"
            )
            for record in descriptor_value.databases:
                run_role_contract(record)
            post_0012 = MEDIA._run_live_audit(
                runner,
                descriptor_value,
                source,
                trusted_image_id=audit_image_id,
            )
            self.assertFalse(post_0012["legacy_relation_present"])
            self.assertEqual(
                post_0012["ledger_analysis"][
                    "canonical_prefix_length"
                ],
                12,
            )
            self.assertEqual(
                post_0012["generation_schema"],
                {
                    "state": "absent",
                    "schema_sha256": None,
                    "schema_authority": None,
                },
            )

            fresh_role = "nexpoly_post_0012_auditor"
            fresh_contract = "sha256:" + "d" * 64
            drop_primary_audit_role(primary_audit_role)
            run_role_contract(
                primary_record,
                role=fresh_role,
                contract_sha256=fresh_contract,
            )
            fresh_databases = tuple(
                {
                    **record,
                    **(
                        {"audit_role": fresh_role}
                        if record["name"] == database
                        else {}
                    ),
                }
                for record in descriptor_value.databases
            )
            fresh_descriptor = replace(
                descriptor_value,
                database_user=fresh_role,
                databases=fresh_databases,
            )
            fresh_audit = MEDIA._run_live_audit(
                runner,
                fresh_descriptor,
                source,
                trusted_image_id=audit_image_id,
            )
            self.assertEqual(
                fresh_audit["role_contract_sha256"],
                fresh_contract,
            )
            self.assertFalse(fresh_audit["legacy_relation_present"])
            drop_primary_audit_role(fresh_role)
            run_role_contract(primary_record)

            admin_sql(
                "CREATE FUNCTION public.nexpoly_policy_called() "
                "RETURNS boolean LANGUAGE plpgsql STABLE AS $$"
                "BEGIN RAISE EXCEPTION 'NEXPOLY_POLICY_CALLED'; END"
                "$$;"
                "CREATE POLICY nexpoly_disabled_hostile ON "
                "governance.schema_migrations USING "
                "(public.nexpoly_policy_called());"
            )
            for enabled in (False, True):
                if enabled:
                    admin_sql(
                        "ALTER TABLE governance.schema_migrations "
                        "ENABLE ROW LEVEL SECURITY;"
                    )
                with self.subTest(hostile_policy_enabled=enabled):
                    with self.assertRaisesRegex(
                        MEDIA.MediaEvidenceError,
                        "refused unsafe migration ledger catalog",
                    ) as captured:
                        MEDIA._run_live_audit(
                            runner,
                            descriptor_value,
                            source,
                            trusted_image_id=audit_image_id,
                        )
                    self.assertNotIn(
                        "NEXPOLY_POLICY_CALLED",
                        str(captured.exception),
                    )
            admin_sql(
                "ALTER TABLE governance.schema_migrations "
                "DISABLE ROW LEVEL SECURITY;"
                "DROP POLICY nexpoly_disabled_hostile ON "
                "governance.schema_migrations;"
                "DROP FUNCTION public.nexpoly_policy_called();"
            )

            for setup, cleanup, expected in (
                (
                    "CREATE TABLE public.nexpoly_ledger_child () "
                    "INHERITS (governance.schema_migrations);",
                    "DROP TABLE public.nexpoly_ledger_child;",
                    "refused unsafe migration ledger catalog",
                ),
                (
                    "CREATE VIEW public.nexpoly_ledger_projection AS "
                    "SELECT * FROM governance.schema_migrations;",
                    "DROP VIEW public.nexpoly_ledger_projection;",
                    "approved owner ordinary table",
                ),
                (
                    "CREATE FUNCTION public.nexpoly_noop_event() "
                    "RETURNS event_trigger LANGUAGE plpgsql AS $$"
                    "BEGIN NULL; END$$;"
                    "CREATE EVENT TRIGGER nexpoly_unapproved_ddl "
                    "ON ddl_command_start EXECUTE FUNCTION "
                    "public.nexpoly_noop_event();",
                    "DROP EVENT TRIGGER nexpoly_unapproved_ddl;"
                    "DROP FUNCTION public.nexpoly_noop_event();",
                    "refused database event triggers",
                ),
            ):
                admin_sql(setup)
                with self.subTest(hostile_catalog=expected):
                    with self.assertRaisesRegex(
                        MEDIA.MediaEvidenceError,
                        expected,
                    ):
                        MEDIA._run_live_audit(
                            runner,
                            descriptor_value,
                            source,
                            trusted_image_id=audit_image_id,
                        )
                admin_sql(cleanup)

            postgres_role = runner.run(
                [
                    MEDIA.DOCKER,
                    "exec",
                    "--user",
                    "postgres",
                    container_id,
                    "psql",
                    "-X",
                    "-A",
                    "-t",
                    "-h",
                    "/var/run/postgresql",
                    "-U",
                    admin,
                    "-d",
                    database,
                    "-c",
                    (
                        "SELECT rolname FROM pg_roles "
                        "WHERE rolname = 'postgres'"
                    ),
                ]
            ).stdout.decode("utf-8", "strict").strip()
            self.assertEqual(postgres_role, "")
            self.assertEqual(
                descriptor_value.online_admin_role,
                admin,
            )
            self.assertEqual(
                bundle["database_identity"]["database"],
                database,
            )
            self.assertEqual(
                sorted(
                    record["name"]
                    for record in bundle["database_inventory"]
                ),
                sorted([database, "postgres"]),
            )
            for command in runner.commands:
                if "psql" not in command or "-U" not in command:
                    continue
                self.assertEqual(
                    command[command.index("-U") + 1],
                    admin,
                )
                if command[1] == "run":
                    self.assertIn("127.0.0.1", command)
                    self.assertNotIn("/var/run/postgresql", command)
                    self.assertIn(f"container:{container_id}", command)
                else:
                    self.assertIn("/var/run/postgresql", command)
        finally:
            runner.run(
                [
                    MEDIA.DOCKER,
                    "container",
                    "rm",
                    "-f",
                    "--",
                    container_id or container_name,
                ],
                check=False,
            )
            runner.run(
                [MEDIA.DOCKER, "volume", "rm", "--", volume],
                check=False,
            )

    def test_dormant_volume_is_copied_audited_and_source_cas_verified(self) -> None:
        major = self.postgres_major()
        image = self.pinned_image()
        runner = MEDIA.CommandRunner()
        volume = MEDIA._temp_name("integration-source")
        initializer_name = MEDIA._temp_name("integration-init")
        label = "dormant-volume-integration"
        source_mount = (
            "/var/lib/postgresql"
            if major == 18
            else "/var/lib/postgresql/data"
        )
        source_subpath = "18/docker" if major == 18 else "."
        initializer: str | None = None
        database_authority: tuple[dict[str, object], ...] | None = None
        try:
            runner.run(
                [
                    MEDIA.DOCKER,
                    "volume",
                    "create",
                    "--label",
                    f"io.nexpoly.test={label}",
                    "--",
                    volume,
                ]
            )
            completed = runner.run(
                [
                    MEDIA.DOCKER,
                    "run",
                    "-d",
                    "--name",
                    initializer_name,
                    "--label",
                    f"io.nexpoly.test={label}",
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    (
                        "/var/run/postgresql:rw,noexec,nosuid,size=16m,"
                        "uid=70,gid=70,mode=0700"
                    ),
                    "--mount",
                    f"type=volume,src={volume},dst={source_mount}",
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
            observed = MEDIA._container_database_inventory(
                runner,
                initializer,
                postgres_major=major,
            )
            self.assertEqual(
                [record["name"] for record in observed],
                ["postgres"],
            )
            database_authority = tuple(
                {
                    **record,
                    "audit_role": "postgres",
                    "migration_scope": "nexpoly-ledger",
                }
                for record in observed
            )
            if major == 18:
                pgdata = runner.run(
                    [
                        MEDIA.DOCKER,
                        "exec",
                        "--user",
                        "postgres",
                        initializer,
                        "/bin/sh",
                        "-ceu",
                        (
                            "test \"$PGDATA\" = "
                            "'/var/lib/postgresql/18/docker'; "
                            "test -f \"$PGDATA/PG_VERSION\"; "
                            "printf '%s\\n' \"$PGDATA\""
                        ),
                    ]
                ).stdout.decode("utf-8", "strict").strip()
                self.assertEqual(
                    pgdata,
                    "/var/lib/postgresql/18/docker",
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
                data_subpath=source_subpath,
                attached=(),
                postgres_major=major,
            )
            descriptor_value = MEDIA.MediaDescriptor(
                media_id=source.media_id,
                kind="docker_volume",
                database="postgres",
                database_user="postgres",
                disposition="retained-private-isolated",
                audit_method="isolated-volume-copy-read-only",
                source_postgres_major=major,
                databases=database_authority or (),
            )
            registry = MEDIA.Registry(
                payload=b"integration",
                digest=MEDIA.sha256_bytes(b"integration"),
                audit_image=MEDIA.POSTGRES_AUDIT_IMAGES[16],
                auditor_sha256=MEDIA._auditor_digest(),
                descriptors=(descriptor_value,),
                required_online_databases=(),
                boundary={},
                audit_images=tuple(
                    sorted(MEDIA.POSTGRES_AUDIT_IMAGES.items())
                ),
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
            self._cleanup_labeled_container_and_volume(
                runner,
                container_name=initializer_name,
                volume_name=volume,
                label=label,
            )

    def test_custom_dump_is_restored_and_audited_in_disposable_pg16(self) -> None:
        if self.postgres_major() != 16:
            self.skipTest(
                "logical custom dumps use the separately fixed PG16 restore"
            )
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

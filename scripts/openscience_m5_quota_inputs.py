"""Stage pinned dev-m1 work records for the quota drill without mounting either original volume."""

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil

import openscience_per_user_dev as manager
from openscience_m5_backup import offline_registry, verify_package


ENVIRONMENT = "dev-m1"
OWNERS = {"a": "test-user-a", "b": "test-user-b"}
HARD_LIMIT_BYTES = 2_000_000_000


def retained_state(registry):
    """Read deployment, lifecycle and Docker volume identity without attaching either volume."""
    manager.local_engine()
    with offline_registry(Path(registry)) as db:
        rows = {}
        for owner in OWNERS.values():
            workspace = db.workspace(ENVIRONMENT, owner)
            if not workspace.resource("volume"):
                raise ValueError("Retained private volume missing")
            rows[owner] = {
                "workspace": workspace.row["id"],
                "uid": workspace.row["uid"],
                "generation": workspace.row["generation"],
                "volume": workspace.volume,
                "created_at": manager.docker(
                    "volume", "inspect", "--format", "{{.CreatedAt}}", workspace.volume
                ),
                "state": workspace.state()["state"],
            }
        return {
            "registry_sha256": hashlib.sha256(db.file.read_bytes()).hexdigest(),
            "workspaces": rows,
        }


def stage_inputs(destination, registry, packages, evidence):
    """Stage synthetic artifacts from private backups, anchored to live ownership and trusted pins."""
    destination, registry, packages, evidence = map(
        Path, (destination, registry, packages, evidence)
    )
    report = json.loads(evidence.read_bytes())
    if (
        report["status"] != "passed"
        or report["environment"] != ENVIRONMENT
        or set(report["users"]) != set(OWNERS.values())
    ):
        raise ValueError("Expected a verified dev-m1 dual-account restore report")
    manager.local_engine()
    staged = {}
    with offline_registry(registry) as db, ExitStack() as streams:
        for role, owner in OWNERS.items():
            expected = report["users"][owner]
            workspace = db.workspace(ENVIRONMENT, owner)
            if (
                expected["package"] != role
                or not workspace.row["initialized"]
                or not workspace.resource("volume")
            ):
                raise ValueError("Expected the original dev-m1 registered volume")
            pin = expected["manifest_sha256"]
            source = packages / role
            meta, archive = verify_package(streams, ENVIRONMENT, owner, source, pin)
            if (
                meta["workspace"] != workspace.row["id"]
                or meta["workspace"] != expected["workspace"]
                or meta["uid"] != workspace.row["uid"]
                or meta["uid"] != expected["uid"]
                or meta["uid"] != expected["gid"]
                or meta["source_volume"] != workspace.volume
                or meta["source_deployment"] != db.data["deployment"]
                or meta["registry_settings"]["image"] != report["workspace_image"]
                or meta["generation"] != expected["source_generation"]
                or meta["archive_sha256"] != expected["archive_sha256"]
                or meta["inventory"] != expected["original_and_restored_inventory"]
            ):
                raise ValueError(
                    f"Backup does not match the registered workspace for {owner}"
                )
            out = destination / role
            out.mkdir(mode=0o700)
            target = out / "workspace.tar"
            with target.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                shutil.copyfileobj(archive, stream)
            with target.open("rb") as copy:
                if (
                    hashlib.file_digest(copy, "sha256").hexdigest()
                    != meta["archive_sha256"]
                ):
                    raise ValueError("Staged archive failed verification")
            manifest_bytes = (source / "manifest.json").read_bytes()
            if hashlib.sha256(manifest_bytes).hexdigest() != pin:
                raise ValueError("Source manifest changed during staging")
            manager.write_atomic(out / "manifest.json", manifest_bytes, 0o600)
            staged[role] = {
                **{
                    k: meta[k]
                    for k in (
                        "environment",
                        "user_id",
                        "workspace",
                        "uid",
                        "inventory",
                        "archive_sha256",
                    )
                },
                "gid": meta["uid"],
                "manifest_sha256": pin,
                "notebook_sha256": expected["notebook_sha256"],
                "archive_generation": meta["generation"],
                "registered_generation": workspace.row["generation"],
            }
    if (
        len({row["uid"] for row in staged.values()}) != 2
        or len({row["workspace"] for row in staged.values()}) != 2
    ):
        raise ValueError("Both accounts require distinct UID and workspace bindings")
    spec = {
        "version": 1,
        "environment": ENVIRONMENT,
        "workspace_image": report["workspace_image"],
        "hard_limit_bytes": HARD_LIMIT_BYTES,
        "users": staged,
    }
    manager.write_atomic(
        destination / "spec.json",
        json.dumps(spec, sort_keys=True).encode() + b"\n",
        0o600,
    )
    return spec

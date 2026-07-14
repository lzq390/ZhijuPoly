from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


MigrationKind = Literal["baseline", "expand", "contract"]
MIGRATION_KINDS = frozenset({"baseline", "expand", "contract"})
MIGRATION_VERSION_PATTERN = re.compile(r"^[0-9]{4}_[a-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class MigrationPolicyEntry:
    version: str
    kind: MigrationKind


def load_migration_manifest(path: Path) -> tuple[MigrationPolicyEntry, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read migration manifest {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Migration manifest {path} must contain a JSON object")
    if payload.get("schema_version") != 1 or not isinstance(payload.get("migrations"), list):
        raise ValueError(f"Migration manifest {path} must use schema_version 1 and contain a migrations list")

    entries: list[MigrationPolicyEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(payload["migrations"]):
        if not isinstance(item, dict):
            raise ValueError(f"Migration manifest entry {index} must be an object")
        version = item.get("version")
        kind = item.get("kind")
        if not isinstance(version, str) or not version or version in seen:
            raise ValueError(f"Migration manifest entry {index} has a missing or duplicate version")
        if MIGRATION_VERSION_PATTERN.fullmatch(version) is None:
            raise ValueError(f"Migration {version} must use the NNNN_lowercase_name format")
        if kind not in MIGRATION_KINDS:
            raise ValueError(f"Migration {version} has invalid kind {kind!r}")
        if set(item) != {"version", "kind"}:
            raise ValueError(f"Migration {version} may only define version and kind")
        seen.add(version)
        entries.append(MigrationPolicyEntry(version=version, kind=kind))
    return tuple(entries)


def validate_migration_manifest(migrations_dir: Path, manifest_path: Path | None = None) -> dict[str, MigrationKind]:
    resolved_manifest = manifest_path or migrations_dir / "manifest.json"
    entries = load_migration_manifest(resolved_manifest)
    sql_versions = {path.stem for path in migrations_dir.glob("*.sql")}
    manifest_versions = {entry.version for entry in entries}
    missing = sorted(sql_versions - manifest_versions)
    stale = sorted(manifest_versions - sql_versions)
    if missing or stale:
        details: list[str] = []
        if missing:
            details.append(f"unclassified SQL migrations: {', '.join(missing)}")
        if stale:
            details.append(f"manifest entries without SQL files: {', '.join(stale)}")
        raise ValueError("Invalid migration manifest: " + "; ".join(details))

    ordered_sql = [path.stem for path in sorted(migrations_dir.glob("*.sql"))]
    ordered_manifest = [entry.version for entry in entries]
    if ordered_manifest != ordered_sql:
        raise ValueError("Migration manifest entries must be in the same lexical order as SQL migration files")
    baseline_versions = [entry.version for entry in entries if entry.kind == "baseline"]
    if len(baseline_versions) != 1 or not entries or entries[0].kind != "baseline":
        raise ValueError(
            "Migration manifest must contain exactly one baseline and it must be the first migration; "
            "post-bootstrap migrations must be expand or contract"
        )
    first_contract = next(
        (index for index, entry in enumerate(entries) if entry.kind == "contract"),
        None,
    )
    if first_contract is not None and any(
        entry.kind != "contract" for entry in entries[first_contract:]
    ):
        raise ValueError(
            "Contract migrations must form the trailing migration suffix; "
            "a new expand after a pending contract requires a separate maintenance migration"
        )
    return {entry.version: entry.kind for entry in entries}


def assert_pending_migrations_allowed(
    pending_versions: list[str],
    migration_kinds: dict[str, MigrationKind],
    allowed_kinds: set[MigrationKind],
) -> None:
    blocked = [
        f"{version} ({migration_kinds[version]})"
        for version in pending_versions
        if migration_kinds[version] not in allowed_kinds
    ]
    if blocked:
        allowed = ", ".join(sorted(allowed_kinds)) or "none"
        raise RuntimeError(
            "Pending migrations are not allowed by this deployment policy: "
            f"{', '.join(blocked)}. Allowed kinds: {allowed}."
        )


def main() -> None:
    default_dir = Path(__file__).resolve().parents[1] / "migrations" / "postgres"
    parser = argparse.ArgumentParser(description="Validate the Postgres migration policy manifest.")
    parser.add_argument("--migrations-dir", type=Path, default=default_dir)
    args = parser.parse_args()
    kinds = validate_migration_manifest(args.migrations_dir)
    counts = {kind: sum(1 for value in kinds.values() if value == kind) for kind in sorted(MIGRATION_KINDS)}
    print(" ".join(f"{kind}={count}" for kind, count in counts.items()))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal


MigrationKind = Literal["baseline", "expand", "contract"]
MIGRATION_KINDS = frozenset({"baseline", "expand", "contract"})
MIGRATION_VERSION_PATTERN = re.compile(r"^[0-9]{4}_[a-z0-9_]+$")
MIGRATION_CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ContractRequirement:
    version: str
    checksum: str


@dataclass(frozen=True, slots=True)
class MigrationPolicyEntry:
    version: str
    kind: MigrationKind
    epoch: int = 1
    checksum: str | None = None
    requires_contracts: tuple[ContractRequirement, ...] = ()
    manifest_schema_version: int = 1


def canonical_migration_checksum(path: Path) -> str:
    """Hash migration SQL after the runner's canonical newline normalization."""

    normalized = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def _require_version(value: object, *, label: str) -> str:
    if not isinstance(value, str) or MIGRATION_VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must use the NNNN_lowercase_name format")
    return value


def _require_checksum(value: object, *, label: str) -> str:
    if not isinstance(value, str) or MIGRATION_CHECKSUM_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a 64-character lowercase SHA-256")
    return value


def load_migration_manifest(path: Path) -> tuple[MigrationPolicyEntry, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read migration manifest {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Migration manifest {path} must contain a JSON object")
    if set(payload) != {"schema_version", "migrations"}:
        raise ValueError(
            f"Migration manifest {path} must contain exactly schema_version and migrations"
        )
    schema_version = payload.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or schema_version not in {1, 2}
        or not isinstance(payload.get("migrations"), list)
    ):
        raise ValueError(
            f"Migration manifest {path} must use schema_version 1 or 2 and contain a migrations list"
        )

    entries: list[MigrationPolicyEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(payload["migrations"]):
        if not isinstance(item, dict):
            raise ValueError(f"Migration manifest entry {index} must be an object")
        version = item.get("version")
        kind = item.get("kind")
        if not isinstance(version, str) or not version or version in seen:
            raise ValueError(f"Migration manifest entry {index} has a missing or duplicate version")
        _require_version(version, label=f"Migration {version}")
        if kind not in MIGRATION_KINDS:
            raise ValueError(f"Migration {version} has invalid kind {kind!r}")
        if schema_version == 1:
            if set(item) != {"version", "kind"}:
                raise ValueError(f"Migration {version} may only define version and kind in schema V1")
            entry = MigrationPolicyEntry(version=version, kind=kind)
        else:
            expected_fields = {
                "version",
                "kind",
                "epoch",
                "checksum",
                "requires_contracts",
            }
            if set(item) != expected_fields:
                raise ValueError(
                    f"Migration {version} must define exactly version, kind, epoch, "
                    "checksum, and requires_contracts in schema V2"
                )
            epoch = item.get("epoch")
            if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
                raise ValueError(f"Migration {version} has an invalid epoch")
            checksum = _require_checksum(
                item.get("checksum"),
                label=f"Migration {version} checksum",
            )
            raw_requirements = item.get("requires_contracts")
            if not isinstance(raw_requirements, list):
                raise ValueError(f"Migration {version} requires_contracts must be a list")
            requirements: list[ContractRequirement] = []
            requirement_versions: set[str] = set()
            for requirement_index, requirement in enumerate(raw_requirements):
                if not isinstance(requirement, dict) or set(requirement) != {
                    "version",
                    "checksum",
                }:
                    raise ValueError(
                        f"Migration {version} contract requirement {requirement_index} "
                        "must contain exactly version and checksum"
                    )
                required_version = _require_version(
                    requirement.get("version"),
                    label=f"Migration {version} contract requirement version",
                )
                if required_version in requirement_versions:
                    raise ValueError(
                        f"Migration {version} repeats contract requirement {required_version}"
                    )
                requirement_versions.add(required_version)
                requirements.append(
                    ContractRequirement(
                        version=required_version,
                        checksum=_require_checksum(
                            requirement.get("checksum"),
                            label=(
                                f"Migration {version} contract requirement "
                                f"{required_version} checksum"
                            ),
                        ),
                    )
                )
            entry = MigrationPolicyEntry(
                version=version,
                kind=kind,
                epoch=epoch,
                checksum=checksum,
                requires_contracts=tuple(requirements),
                manifest_schema_version=2,
            )
        seen.add(version)
        entries.append(entry)
    return tuple(entries)


def validate_migration_manifest_entries(
    migrations_dir: Path,
    manifest_path: Path | None = None,
) -> tuple[MigrationPolicyEntry, ...]:
    """Validate and normalize either manifest generation into exact entries."""

    resolved_manifest = manifest_path or migrations_dir / "manifest.json"
    loaded_entries = load_migration_manifest(resolved_manifest)
    sql_paths = sorted(migrations_dir.glob("*.sql"))
    sql_versions = {path.stem for path in sql_paths}
    manifest_versions = {entry.version for entry in loaded_entries}
    missing = sorted(sql_versions - manifest_versions)
    stale = sorted(manifest_versions - sql_versions)
    if missing or stale:
        details: list[str] = []
        if missing:
            details.append(f"unclassified SQL migrations: {', '.join(missing)}")
        if stale:
            details.append(f"manifest entries without SQL files: {', '.join(stale)}")
        raise ValueError("Invalid migration manifest: " + "; ".join(details))

    ordered_sql = [path.stem for path in sql_paths]
    ordered_manifest = [entry.version for entry in loaded_entries]
    if ordered_manifest != ordered_sql:
        raise ValueError("Migration manifest entries must be in the same lexical order as SQL migration files")

    entries: list[MigrationPolicyEntry] = []
    for entry, path in zip(loaded_entries, sql_paths, strict=True):
        actual_checksum = canonical_migration_checksum(path)
        if entry.checksum is not None and entry.checksum != actual_checksum:
            raise ValueError(
                f"Migration {entry.version} manifest checksum {entry.checksum} "
                f"does not match canonical SQL checksum {actual_checksum}"
            )
        entries.append(replace(entry, checksum=actual_checksum))

    baseline_versions = [entry.version for entry in entries if entry.kind == "baseline"]
    if len(baseline_versions) != 1 or not entries or entries[0].kind != "baseline":
        raise ValueError(
            "Migration manifest must contain exactly one baseline and it must be the first migration; "
            "post-bootstrap migrations must be expand or contract"
        )

    schema_versions = {entry.manifest_schema_version for entry in entries}
    if schema_versions <= {1}:
        first_contract = next(
            (index for index, entry in enumerate(entries) if entry.kind == "contract"),
            None,
        )
        if first_contract is not None and any(
            entry.kind != "contract" for entry in entries[first_contract:]
        ):
            raise ValueError(
                "Contract migrations must form the trailing migration suffix; "
                "a new expand after a pending contract requires migration manifest schema V2"
            )
        return tuple(entries)

    epochs = [entry.epoch for entry in entries]
    if epochs[0] != 1:
        raise ValueError("Migration epoch numbering must start at 1")
    for previous, current in zip(epochs, epochs[1:]):
        if current < previous or current > previous + 1:
            raise ValueError("Migration epochs must be ordered and contiguous")

    by_version = {entry.version: entry for entry in entries}
    prior_contracts: list[ContractRequirement] = []
    for epoch in sorted(set(epochs)):
        epoch_entries = [entry for entry in entries if entry.epoch == epoch]
        first_contract = next(
            (index for index, entry in enumerate(epoch_entries) if entry.kind == "contract"),
            None,
        )
        if first_contract is not None and any(
            entry.kind != "contract" for entry in epoch_entries[first_contract:]
        ):
            raise ValueError(
                f"Contract migrations must form the trailing suffix of epoch {epoch}"
            )
        expected_requirements = tuple(prior_contracts)
        for entry in epoch_entries:
            if entry.requires_contracts != expected_requirements:
                raise ValueError(
                    f"Migration {entry.version} must require every contract from earlier epochs "
                    "with its canonical checksum"
                )
            for requirement in entry.requires_contracts:
                required = by_version.get(requirement.version)
                if (
                    required is None
                    or required.kind != "contract"
                    or required.epoch >= entry.epoch
                    or required.checksum != requirement.checksum
                ):
                    raise ValueError(
                        f"Migration {entry.version} has an invalid contract requirement "
                        f"{requirement.version}"
                    )
        prior_contracts.extend(
            ContractRequirement(entry.version, str(entry.checksum))
            for entry in epoch_entries
            if entry.kind == "contract"
        )
    return tuple(entries)


def validate_migration_manifest(migrations_dir: Path, manifest_path: Path | None = None) -> dict[str, MigrationKind]:
    entries = validate_migration_manifest_entries(migrations_dir, manifest_path)
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

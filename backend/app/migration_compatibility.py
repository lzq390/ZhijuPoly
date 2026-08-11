"""The exact forward ledger states intentionally readable by bridge B.

This is not a generic "ignore unknown migrations" escape hatch.  B accepts
only F's checksum-exact registered prefixes after every B migration through
the checksum-exact 0012 contract is present.
"""

from __future__ import annotations

from collections.abc import Mapping


FORWARD_COMPATIBLE_MIGRATION = {
    "version": "0013_monomer_dft_jobs",
    "checksum": (
        "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc"
    ),
}
FORWARD_COMPATIBLE_MIGRATIONS = (
    FORWARD_COMPATIBLE_MIGRATION,
    {
        "version": "0014_monomer_md_task_queue_cancel",
        "checksum": (
            "7d91b451371eaf10542440c8b947c9ac50b51e3d553cb205a76aca196eaf8df6"
        ),
    },
    {
        "version": "0015_property_filter_performance",
        "checksum": (
            "e0159576c09d31de8a7da46f728d36553f67aa75adba344f93cdc302cf000732"
        ),
    },
)


def compatible_forward_versions(
    applied: Mapping[str, str],
    canonical_checksums: Mapping[str, str],
) -> frozenset[str]:
    """Classify an exact registered B-ledger forward prefix."""

    registered = {
        record["version"]: record["checksum"]
        for record in FORWARD_COMPATIBLE_MIGRATIONS
    }
    extra = set(applied).difference(canonical_checksums)
    ordered_versions = tuple(record["version"] for record in FORWARD_COMPATIBLE_MIGRATIONS)
    valid_prefixes = {
        frozenset(ordered_versions[:index])
        for index in range(1, len(ordered_versions) + 1)
    }
    valid_extra = frozenset(extra) in valid_prefixes
    if (
        not valid_extra
        or any(applied.get(version) != registered[version] for version in extra)
        or set(canonical_checksums).difference(applied)
        or any(
            applied.get(name) != expected
            for name, expected in canonical_checksums.items()
        )
    ):
        return frozenset()
    return frozenset(extra)


def require_known_or_exact_forward_ledger(
    applied: Mapping[str, str],
    canonical_checksums: Mapping[str, str],
) -> frozenset[str]:
    """Reject every unknown ledger except a registered complete F prefix."""

    unknown = set(applied).difference(canonical_checksums)
    if not unknown:
        return frozenset()
    compatible = compatible_forward_versions(applied, canonical_checksums)
    if compatible != unknown:
        raise RuntimeError(
            "Migration ledger contains versions absent from the canonical manifest: "
            + ", ".join(sorted(unknown))
        )
    return compatible

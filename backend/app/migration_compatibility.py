"""The single forward ledger state intentionally readable by bridge B.

This is not a generic "ignore unknown migrations" escape hatch.  B accepts
only F's checksum-exact 0013, and only after every B migration through the
checksum-exact 0012 contract is present.
"""

from __future__ import annotations

from collections.abc import Mapping


FORWARD_COMPATIBLE_MIGRATION = {
    "version": "0013_monomer_dft_jobs",
    "checksum": (
        "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc"
    ),
}


def compatible_forward_versions(
    applied: Mapping[str, str],
    canonical_checksums: Mapping[str, str],
) -> frozenset[str]:
    """Classify the exact B-ledger-plus-0013 state, otherwise return empty."""

    version = FORWARD_COMPATIBLE_MIGRATION["version"]
    checksum = FORWARD_COMPATIBLE_MIGRATION["checksum"]
    extra = set(applied).difference(canonical_checksums)
    if (
        extra != {version}
        or applied.get(version) != checksum
        or set(canonical_checksums).difference(applied)
        or any(
            applied.get(name) != expected
            for name, expected in canonical_checksums.items()
        )
    ):
        return frozenset()
    return frozenset({version})


def require_known_or_exact_forward_ledger(
    applied: Mapping[str, str],
    canonical_checksums: Mapping[str, str],
) -> frozenset[str]:
    """Reject every unknown ledger except the unique complete F/0013 state."""

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

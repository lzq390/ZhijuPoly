# Local literature governance foundation

This local dependency group builds on reviewed MD c2d5383c and the separate
literature normalizer 509de18d. The planned 0024 slot is conditional on that MD
0023 prefix; remote main and target database history must be rechecked before
any later integration or migration. No database was contacted or migrated.

The finite ledger contract preserves every historical migration checksum,
rejects missing usage/entitlement/MD history, and only allows the registered
0023-to-0024 extension. Historical exact prefixes remain exact; a caller cannot
invent matching observed/expected lists to admit an unknown ledger.

Backups include all three literature projection tables if present and reject
partial inventories. Existing structure, usage, entitlement and MD handling is
preserved. The optional SQL retains ordered generation locks and rejects every
pre-existing maintenance group until a separate provenance review, rather than
adopting unverified ACLs or inherited authority. Re-running the raw SQL is not
supported; normal migration identity is tracked by the canonical ledger.

This is not runtime admission: automatic migrations always defer 0024, and the
existing application/backup-schema validators continue to reject 0024. Wiring
the full feature needs exact runtime schema/ACL/trigger and audit/restore
contracts plus genuine PostgreSQL tests. No API or UI is enabled here.
No claim of real PostgreSQL concurrency, applied migration, or scientific
acceptance is supported by the synthetic inventory and policy tests.

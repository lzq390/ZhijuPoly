# Exact literature migration source compatibility

Legacy isolation v2 remains exact through 0019. A distinct standalone validator
recognizes only the complete pinned 0001-0024 ledger, including every main
structure/usage/entitlement/MD migration; no skipped, reordered, duplicate or
unknown records are accepted. The existing finite bridge remains only 0023 to
0024. The old test that appended 0024 directly to 0019 described an invalid state.

Preflight checksum knowledge uses a copied release cache and the pinned 0024
fact for isolation profiles. It does not change required schema boundaries,
startup/automatic migration epochs, role checks, entitlement checks or current
application admission. Future 0024 application admission still fails closed.

This group tests source-only validators and fake preflight contracts. It applies
no SQL and does not establish PostgreSQL permissions, concurrency, asset access,
API readiness, backup restoration or production deployment correctness.

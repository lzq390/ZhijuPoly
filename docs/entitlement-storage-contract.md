# Entitlement persistence and release contract

This implementation is development-only. No running database migration, policy installation, tier change, push, merge, or deployment is implied by the code or isolated test results.

## Compatibility and provisional migration numbers

The current feature tree uses `0020_usage_metering` (epoch 5) and `0021_usage_entitlements` (epoch 6). Another feature owns the structure-library migration slot. These numbers are provisional until the parent integration task supplies its final committed SQL and checksum. Never copy an unapproved migration or silently accept a future ledger.

After an approved renumber, update together:

- SQL filenames and the validated migration manifest, including exact contract dependencies.
- `task_usage.entitlements.MIGRATION_VERSION`, application/readiness/backup constants, and intentional historical-prefix test targets.
- `task_usage/entitlement_release.json`: the complete ordered release-known migration ledger and the entitlement SQL checksum.
- The worker/backend package copies, ensuring the JSON is shipped next to `entitlement_access.py`.
- Full-chain readiness, worker release-contract equality, tamper rejection, and real dump/restore tests.

The JSON is a non-secret packaged release contract, not a runtime credential. Workers compare it with the actual ledger and reject unknown or modified entries. Backend startup also validates the full schema, privileges, RLS, and trigger contract. A matching ledger alone is insufficient.

## Authority and transaction boundaries

The five entitlement tables live in a new schema. All functions execute with invoker privileges; no SECURITY DEFINER function or expansion of existing auth permissions is introduced. API queries combine server-resolved owner filtering with FORCE RLS. Business deletion has no cascading relationship to entitlement operations.

Quota hooks default off and then perform no SQL. Explicit service/admin policy installation or tier configuration is a separate administration operation. No public registration or self-upgrade mutation endpoint is provided.

Access tiers and saved results are independent of quota-hook enablement. `resolve_access_tier` remains active when quota recording is disabled. A missing entitlement migration/table preserves pre-cutover authentication initialization; a ledger that claims the migration while its table is absent is rejected. Database/readiness errors are never treated as member access. Existing users without an explicit assignment retain member; free requires explicit configuration.

Tier configuration requires an existing non-system account. It locks that row in `auth.users` using existing permissions before the entitlement owner lock. Native first-start authorization holds the same account lock, so start and downgrade have a defined order. Already authorized/running work is not automatically stopped or refunded. Settlement and reservation do not acquire auth/account locks.

Connection-injected operations never commit or open another connection. Reservation, native binding, result persistence, and settlement can therefore participate in the business transaction. Unknown execution retains its reservation; HTTP timeout, cancellation requested, or a missing local cache entry alone is not proof that work stopped.

## Accounting and period rules

Request observations, successful dispatch claims, and qualified successes are separate counts. A duplicate HTTP request is not another execution. Exact idempotency-key replay recovers the original operation before current policy/capacity checks; a conflicting request fingerprint is rejected.

UTC day, ISO-Monday week, and calendar month periods are supported. Each operation freezes its admission window and policy version. Same owner/pool/window accounting spans policy versions. Changing cadence is rejected while a non-released overlapping old window remains open; it cannot reset consumed capacity. Active reservations, including unknown work from old windows and other pools, count against one owner-wide active limit.

Summaries expose old-window held/unknown reservations separately from current-window usage. They do not silently free these reservations. Changing a policy is not a refund or a replay authorization.

Result JSON is owner-private, bounded by the frozen policy, and stored atomically with settlement. Saved-result reads remain available with recording disabled after exact schema validation. Native deletion does not make the old idempotency key eligible for fresh work.

## Verification boundary

Tests run against nonce-owned PostgreSQL 16 containers and synthetic users under real API/service roles. The harness uses network-none PostgreSQL, a test container sharing only that namespace, explicit runc, no GPU, no host ports or runtime DSNs, bounded CPU/memory, and offline matching dump/restore tools. Evidence records source hashes and exact cleanup. Historical fixtures explicitly apply their intended released prefix; complete-chain tests continue to cover the new migration. No production or real development database is used.

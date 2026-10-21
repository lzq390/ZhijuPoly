# Background usage collection

Collection is opt-in through `USAGE_METERING_ENABLED=true`. Disabled hooks make
no usage database queries or writes. Pricing, admission limits, cancellation,
worker resource guards, and existing owner/RLS rules remain unchanged. All 21
catalog modules have **partial** coverage; the per-module metric table is in
[usage-metering.md](usage-metering.md). OpenScience is an additional external
service, not the only gap.

## Persistent tasks and transaction boundaries

MD, DFT, online knowledge, and polymerization batch observations use the same
connection and transaction as the business transition. Enabled ledger failures
propagate and roll back that transaction; existing domain recovery/fencing stays
authoritative. This is atomic repository behavior, not a promise that every
native HTTP route converts every database error to 503. No resource is released
or lifecycle guard bypassed to make accounting succeed.

Owners come from authenticated native submission and durable domain rows, not
headers. Stable namespaced IDs and attempt ordinals deduplicate the same event
payload. DFT uses its durable attempt ordinal; network redispatch retains it.
MD currently uses one attempt per job. Batch chunks have execution-kind child
records and a new ordinal on each claim. Online providers inherit the persisted
root identity; mutable/deletable online history is not an accounting source.
Business deletion leaves the ledger intact. Batch retention `expired` is not a
new execution outcome.

Native HTTP submission has no extra request root. Rejection before the
repository/manager hook, including some validation, configuration, or capacity
checks, is not recorded. In contrast, an authenticated synchronous request starts
a root before business handling and can record its later 4xx rejection. These
counts are not a uniform total of all attempted submissions. Client retry makes
a new synchronous request UUID; native retry keeps a task only where the existing
business idempotency contract does so. Stable event keys do not provide global
exactly-once request handling.

## In-memory tasks and failures

Conditional generation, PolyTAO, and reverse design keep their native lifecycle.
Submission captures owner, task identity, and collector for the executor. Attempt
identity includes the backend boot UUID. Terminal collection follows the native
Future outcome. An outage after execution retains exact terminal metadata in the
collector's in-memory pending queue, marks metering degraded, and does not block
native cleanup. The next real collector write retries that metadata. Admission
writes fail before dispatch; the pending queue is not durable across restarts.

Hard process loss can destroy native jobs and pending metadata. A lost commit
acknowledgement can also leave persisted state that the caller cannot confirm.
There is no comprehensive startup reconciliation for either case, and no global
advisory-lock heuristic that marks old instances failed. Unproven observations
stay at their last recorded status; neither missing duration nor tokens are
replaced with zero. Client disconnection alone does not prove computation ended.

Explicit 3D deadline and GPU queue-timeout exceptions are annotated timed_out
without changing their original HTTP 422/503 contracts. Generic 422/503 and queue
full/stopped are not reclassified by that annotation. An unsuccessful child that
was recovered does not automatically fail its root; interrupted/cancelled retain
priority. Reverse-design deadline/row-limit outcome mapping is not expanded by
this change.

## Native online startup recovery

Only the existing single-backend startup recovery, before accepting submissions,
provides this reconciliation boundary. Its business UPDATE RETURNING identifies
the exact recovered owners and roots. In the same transaction, accounting marks
those roots interrupted and appends `source=reconciliation`, `interrupted` events
to their still queued/running provider children. Other roots, owners, terminal
children, and execution-kind children are not changed by that child reconciliation.

The child finish timestamp is when native recovery discovered the lost local
observer. It does not prove the external provider stopped then. Recovery supplies
no new duration or token metrics; existing known values stay known and missing
ones stay NULL. If ledger writing fails, the business recovery and usage events
roll back together. This is not a new daemon or a global cross-module repair pass.

## Duration meaning and metric gaps

Root `observed_wall_ms` measures submission/attempt creation to the observed native
terminal state, including queueing. MD/DFT persistence and reconciliation times
are not measurements of scientific execution. A terminal-only DFT snapshot does
not establish a real start. MD alone currently writes `queue_ms`, from database
creation to its first running timestamp; this is not a separately measured Broker
GPU wait. Planned MD step totals are not observed completed work.

Batch and online root wall uses business `created_at` through the stored terminal
or recovery observation time. On restart it can include downtime until discovery.
Re-observing an old terminal row with NULL metrics uses the stable `root-wall-v1`
event key for enrichment, preserving the old event and the same root/attempt.
There is no automatic historical backfill.

Native GPU manager running means its executor entered and may still be waiting
for GPU admission. Its root leaves the physical start/execution duration unknown.
Conditional generation and PolyTAO have separate local model execution spans;
reverse design currently has root wall only. Execution spans are wall-clock
intervals around instrumented model calls, not CPU time or GPU utilization.

Batch chunk observed wall includes preparation, execution, result publication and
safety cleanup. Its `execution_ms` stays NULL. Recovery's `consumed_seconds` can
be bounded by the last heartbeat and is not exported as measured usage; interrupted
chunk elapsed time remains unknown. Neither child wall nor model execution time
is added to root wall in the summary.

Provider children record external-call wall and only token fields actually
returned. Literature HTTP children do not have token usage, so token unknown
counts include not-applicable calls. Assistant Chat streams currently do not add
`include_usage`; their token coverage can be missing even after a successful
answer. No text-length estimate or extra provider call fills that gap.

There is no production `artifact_bytes` writer yet. Process CPU time, GPU time,
utilization, current disk occupancy, and money are not collected. The panel is
partial observation, not a resource bill or a complete inventory of compute.

## Validation boundary

Disposable real PostgreSQL tests use synthetic users/jobs and real repository
transactions, including insertion failures, atomic rollback, owner separation,
and repeated callbacks. They do not dispatch scientific computation. Lifecycle
and provider tests use mocks; UI checks use fixture responses. These tests do not
constitute all-feature live model/provider/GPU end-to-end verification or proof of
hard-crash/ACK-loss recovery. The current runtime databases remain unmigrated for
this feature. Permission, migration, backup, and rollout requirements remain as
documented in [usage-metering.md](usage-metering.md).

## Separate opt-in base-count eligibility

The entitlement policy is separate from usage observation and remains disabled
by default. `task_usage.entitlement_policy` performs pure version/spec/fact checks;
it does not reserve, settle, authenticate, read configuration files, or start work.
See the eight-pool example and exact decision contract in
[usage-metering.md](usage-metering.md). The example budgets are synthetic fixtures,
not approved commercial allowances. Existing account permissions remain authoritative.

Native adapters must bind the server-derived specification and full private request
fingerprint to the operation before dispatch. The repository pins immutable policy
version/hash and derives its own pool. One bounded root can settle at most one base
unit: generation microbatches, provider attempts and batch chunks are not additional
base charges. Idempotent delivery uses the durable operation/result receipt, never
just a last-observed usage status. A client retry with a new request identity is not
proof that prior work is absent.

Qualification consumes typed server evidence, not raw HTTP success. Generation
uses `candidate_count` as the target (`top_k` controls sampling); at least one valid
candidate can be qualified with an explicit partial label under the example rule.
Batch partial output defaults to zero. DFT non-convergence may still be a delivered
service when all requested properties/reports are present, but is never called
scientific success. MD formal evidence is protocol/report completion plus original
cleanup confirmation, not an estimated step count; native demo has its own spec and
zero-unit example rule. Online counts use actual extraction success/failure facts,
not table row counts, and example data is labeled demo.

`terminal=False`/unknown means preserve reservation and evidence for exact-operation
recovery. Zero units in an unknown decision is not permission to release or resubmit.
An explicit completed refusal/clarification/cache/no-events result is different from
an unproven EOF. No policy decision releases GPU resources, alters lifecycle fencing,
changes existing cancellation semantics, or declares a crashed process dead.

Pure policy tests establish these decisions on synthetic inputs. They do not replace
repository concurrency/rollback tests, actual bounded science validation, or end-to-end
runtime testing. No running database, main checkout, or deployed policy is changed by
this development work.

The example adds `request_rate_per_minute=120` for per-process HTTP protection and
`active_operations_per_owner=4` for the repository's durable owner-wide admission
check. These are operational bounds, not approved monthly allowances. The owner
lock must cover all pools and uncertain reservations; per-pool locking alone is
insufficient. Process-local rate state is not a cross-instance quota authority,
and a rate_only policy label does not itself implement a limiter. Existing worker,
GPU and queue guards continue to apply.


The immutable policy selects UTC `day`, ISO `week` (Monday 00:00), or calendar
`month`; the example remains monthly. Admission pins the exact start/end window,
including completion after its end. Same owner/pool/window consumption is shared
across policy versions. A different window boundary overlapping an unexpired,
non-released admission is rejected as `period_policy_conflict`; switching versions
or cadence cannot reset that consumption. Old-window active/unknown holds still
count toward owner concurrency and are exposed as `held_outside_current_period`.
These repository guarantees require the separate real-PG tests; pure policy tests
only establish accepted period values and immutable policy hash changes.

Property-similarity `query` runs one actual predictor call and uses
`chemistry_small`. Its strict specification is `match_mode=property`,
`property_count=1`, bounded `top_k`, and `similarity_threshold`. Structure queries
use `match_mode=structure` plus the same search bounds and remain rate-only;
legacy exact/similarity request modes are normalized before policy selection.
A validated finite prediction and completed search are required. A legitimate
empty match uses the configurable chemistry no-match outcome, while a missing
prediction is partial/zero and an unproven completion retains its reservation.
Neither the prediction child nor the query root is billed a second time for the
same bounded operation. Private query/property inputs stay in the request
fingerprint, not in public specifications or qualification facts.

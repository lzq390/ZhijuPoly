# Background usage collection

This implementation is opt-in through `USAGE_METERING_ENABLED=true`. The default
is disabled: hooks perform no usage database queries or writes. It does not
change pricing, admission limits, cancellation support, or worker resource guards.

## Persistent tasks

MD, DFT, online knowledge, and polymerization batch records use the same database
connection and transaction as the existing business transition. The enabled
ledger must be installed and writable; failures propagate and roll back that
transaction, leaving the existing domain recovery/fencing rules authoritative.
Owners come from domain rows, not caller-supplied headers. Stable namespaced task
identifiers and attempt ordinals make re-observation idempotent. The usage ledger
is independent of business deletion. Batch retention `expired` is not emitted as
a new execution outcome.

DFT uses its actual attempt ordinal; network redispatch is the same attempt. MD
currently has one attempt per job. Batch chunks have separate execution-kind
records linked to their parent task, with a separate ordinal for each claim.
Online history is not a usage source: it overwrites equivalent searches and can
be deleted. Online background providers inherit the persisted task identity.

## In-memory tasks

Conditional generation, PolyTAO, and reverse design retain their native lifecycle.
Submission captures immutable owner/task identity and the collector, and restores
that context in the executor. Their attempt identity contains the backend boot
instance UUID. Terminal collection runs after the native Future outcome is known.
A terminal ledger outage retains the exact timestamp/key/absolute totals in the
collector pending queue, marks metering degraded, and does not block native task
cleanup. Admission writes fail visibly before execution. Deferred metadata is
retried by the next real collector write; it is not a durable outage queue.

An abrupt process loss can destroy both the native job and pending metadata.
There is deliberately no global startup rewrite or advisory-lock heuristic that
marks all old jobs failed. An unproven old instance stays at its last observation;
coverage is incomplete until an operator or native lifecycle proves that exact
instance has ended. No duration is fabricated for that gap.

## Duration meaning

Native GPU manager `running` means its executor has begun; it can still be waiting
for GPU admission. Its root record therefore leaves physical `started_at` and
`execution_ms` unknown. Root `observed_wall_ms` measures submission to the observed
native terminal result, including waiting. Fine-grained scientific/provider
execution spans are recorded separately; do not sum them with root wall time.

MD/DFT database timestamps and worker reconciliation are not GPU utilization
measurements. Formal MD step totals can be planned/estimated, not observed steps.
DFT terminal-only observations do not establish a real start time. Unavailable
measurements remain NULL, never zero.

Batch chunk `observed_wall_ms` surrounds preparation, child execution, result
publication and safety cleanup. It is not CPU time or a pure scientific kernel
interval, so `execution_ms` remains NULL. Recovery's cumulative `consumed_seconds`
may stop at the last heartbeat; that estimate is not exported as a measured usage
duration. A recovered interrupted chunk retains unknown elapsed time.

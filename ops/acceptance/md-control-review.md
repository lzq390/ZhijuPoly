# Independent MD: prepared source, not a live admission

The runtime may reference the one already provisioned native acceptance
`backend.auth.env` in the old `r2-science-20261016-ab36-fix1` cohort. Preparation
uses `O_PATH` metadata only and pins the exact path, owner, mode, single link,
device/inode, size, mtime and ctime. No credential byte is copied, hashed,
displayed or exported. The finite cohort schema permits this exception only
for the exact new worktree and `r2-a-20261019` cohort; all other paths keep the
previous confinement rule. Native Compose/systemd/application consumers load
values at runtime. The independent controller verifies the metadata receipt
before admission and still requires the exact acceptance database. File
rotation or replacement fails closed. Metadata readiness does not prove DSN,
role, ledger, schema or owner-session readiness, which remain native gates.

`scripts/acceptance_md_control.py` defaults to the public dry-run plan. Its
`seal`, `watch`, `supervise` and `cleanup` modes have not been executed. There is
no new scientific PASS. Actual launch remains blocked until the action-time
prerequisites below have real receipts.

The short GPU batch must restore Main by 55 minutes and prove the original
native-direct source/mode by 60 minutes. The independent attachment starts only
after that restore. It reuses Main's already running GPU1-only Broker and MPS;
it does not keep the private cohort or a replacement Main alive. Its worker ID,
socket, job directory and user services are unique to this scope.

The initial and trailing inventories use the same explicit native Root client
factory, pinned to the new candidate commit/tree. Native MPS descriptor, Docker,
host-scope, PID/start/cgroup and client attribution functions are reused. A
changed Broker peer/socket/instance, MPS root, inventory, native source, Root
epoch or audit freshness stops this attachment. Three read-only audit attempts
are allowed; this never retries a scientific submission. This strict policy may
stop on persistent legitimate lifecycle churn; no live stability claim is made.

The action-time packet must contain these exact public, owner-owned files under
the new `md-independent` runtime directory:

- `activation.public.json`: fixed schema accepted by `load_activation`, actual
  source/lock, Main restore, Broker peer/socket and Root guard/provider bindings.
- `resources.public.json`: all eight enforced long-phase allocations, measured
  total owned storage, source/object IDs and a fresh timestamp.
- `owner-sessions.public.json`: exact A/B public owner IDs, authenticated,
  must-change=false, 36-hour policy, fresh timestamp; no cookies/session export.
- `scientific-assets.public.json`: actual immutable native ByteFF2/OpenMM paths
  and source/asset/venv evidence, fresh native preflight and timestamp.

The latter three are hash-bound by `activation.public.json`. This preparation
does not create a positive receipt or populate a secret environment file. The
native existing service DSN must be supplied opaquely as
`APP_SERVICE_POSTGRES_DSN` at runtime; it is never a command argument, public
document, or diagnostic output. Do not reuse a production identity, grant a
service role new rights, or read/export old private environment files. Runtime
SELECTs use the native repository and only this exact worker's jobs.

`seal` is one-shot. Its nonce identifies a worker process record and does not
create time credit. The deadline is clamped to the already sealed Root deadline
with a separate 300-second reserve, and to the fixed 40-hour approval deadline.
Transport still requires 83,100 seconds remaining: 79,200 execution, 300 setup,
3,600 final cleanup. No protocol, atom count, step count or output requirement
is reduced. Native numerical checkpoint/live-step support remains unchanged.

Start the fixed audit user unit first, then the fixed MD supervisor user unit.
Both need `Restart=no`, `RuntimeMaxSec=93600`, `TimeoutStopSec=300`,
`NoNewPrivileges=yes`, `MemorySwapMax=0`, `UMask=0077`, and a fixed
`ExecStopPost=/usr/bin/python3 -I -B <fixed-source>/scripts/acceptance_md_control.py cleanup`.
Use an existing legal, owner-controlled opaque runtime environment for the DSN;
do not put its value in `systemd-run` arguments. The supervisor unit contains
the Worker child; the independent watcher cleans it if supervision stops.

The long-phase aggregate proposal, including retained CPU/preview and both Root
processes, is:

| Owned target | CPU quota | RAM | Swap |
|---|---:|---:|---:|
| CPU backend | 100% | 3 GiB | 0 |
| Preview | 50% | 0.5 GiB | 0 |
| MD supervisor and Worker | 100% | 1.5 GiB | 0 |
| One native MD execution scope | 400% | 8 GiB | 0 |
| Independent audit | 25% | 0.25 GiB | 0 |
| Root finite guard | 10% | 0.125 GiB | 0 |
| Original Root inventory provider | 100% | 0.25 GiB | 0 |
| Native read-only status publisher | 5% | 0.09375 GiB | 0 |
| **Aggregate** | **790%** | **13.71875 GiB** | **0** |

The original provider's 100%/256MiB limits are included without changing its
unit. The stopped legacy CPU container must be retained inert for rollback; it cannot run concurrently with this long phase. This is a proposal within 8CPU/16GiB, not evidence that all limits are
currently enforced. Any other retained acceptance process must be included
before admission; no build/test container or Home instance runs concurrently
with this phase unless the complete inventory still fits. Total owned storage
must fit 24GiB, including retained artifacts and the isolated database. Exact
current database/storage accounting and real MD peak behavior remain unproved.

Cleanup reads only exact worker-owned IDs (including terminal jobs for lease
attribution). It posts a durable single-attempt native cancel intent, then uses
the existing native process record plus pidfd to terminate only the Worker.
It waits for the exact requests/probe/worker-owner leases to disappear. Unknown
global waiters, quarantine or ambiguous probes block a positive cleanup receipt;
they do not authorize a global Broker drain or cancelling another actor. Native
Broker release remains responsible for workload/MPS/cgroup cleanup. Root loss,
native repository unavailability, changed process identity or cleanup timeout
is reported as a blocker, never hidden as successful retirement.

The fixed user units and their resource/ExecStopPost properties still need to
be materialized from the final source/hash packet, alongside the opaque owner
runtime environment. These are action-time operations and have not run here.
Do not treat the offline tests as proof of real worker launch, systemd timers,
failure delivery, continuous 22-hour audit or selective physical retirement.

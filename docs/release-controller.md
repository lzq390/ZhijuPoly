# NexPoly pull-deployment controller

The production controller applies one reviewed `origin/main` commit to the
dedicated production checkout. It is installed under the external runtime root
so replacing checkout files cannot replace the process that is coordinating the
deployment.

## Fixed identities

```text
source checkout  /data/lzq/gith/nexpoly
runtime root     /data/lzq/gith/nexpoly-runtime
controller                  /data/lzq/gith/nexpoly-runtime/bin/nexpoly-pull-deploy
readiness                   /data/lzq/gith/nexpoly-runtime/bin/nexpoly-production-readiness
historical alias repair     /data/lzq/gith/nexpoly-runtime/bin/nexpoly-reconcile-production-0005-polytao-alias
historical bridge recovery  /data/lzq/gith/nexpoly-runtime/legacy-takeover/bin/nexpoly-bridge-recover
configuration    /data/lzq/gith/nexpoly-runtime/config
state            /data/lzq/gith/nexpoly-runtime/state
audit            /data/lzq/gith/nexpoly-runtime/audit
backups          /data/lzq/gith/nexpoly-runtime/backups
wheel cache      /data/lzq/gith/nexpoly-runtime/wheel-cache
Worker slots     /data/lzq/gith/nexpoly-runtime/worker-venvs
```

The controller accepts only a full lowercase SHA reachable from the fetched
`origin/main` and a unique safe operation ID. It never accepts a branch, tag,
short SHA, image name, migration name, asset path, database password or Worker
slot from the operator.

The requested SHA binds:

- the Git commit and tree hash;
- the successful protected-main CI checks;
- Backend and Web SHA tags, their resolved digests and OCI labels;
- `release-input.json` and its asset digest;
- the migration policy manifest and canonical SQL checksums;
- MD and DFT Worker lock hashes and prepared environment identities.

## Current production authority and prerequisites

The current production runtime was brought under control by the completed
`manual-runtime-adoption` path. Its original source, images, PostgreSQL
identity/ledger, assets, MD/DFT identities, and manual provenance remain sealed
in `adopted-deployment.json`; `bootstrap-control.json` is schema v3. This is an
already adopted installation, so the historical takeover and F→B bridge are
not rerun for an ordinary release.

Before the first descriptor-v4 release at a new reviewed SHA, a private,
standalone, clean, exact-main SSH clone must run the one-time
`adopt_runtime_prerequisites.py plan/apply` transaction. It installs only
source-pinned helpers and the mutable-audit service file, preserves the private
pgpass, and publishes `adopted-prerequisites.json`; it does not touch Git,
PostgreSQL, containers, services, or credentials. Its `abort` subcommand can
remove only operation-owned files before the authority commit. See
[deployment.md](deployment.md) for the parser-exact commands.

The same exact source then runs
`provision_mutable_data_audit_role.py --plan/--apply`. Formal Pull `plan` and
`prepare` are forbidden until that transaction completes. The resulting
mutable-data audit helper report is schema-v7 and proves an explicit
least-privilege projection: no elevated role attributes, memberships,
ownership, cluster-wide predefined read role, write authority, or authority
outside the governed schemas. The only normalized direct grants are
database `CONNECT`, governed schema `USAGE`, current table/view and sequence
`SELECT`, and `polyprop`'s schema-scoped future `SELECT` defaults; direct
function grants and security-definer execution are absent. Apply separately
confirms the reviewed impact of revoking PUBLIC execution from the eight
large-object mutators. No secret or plaintext password is a command argument
or audit value.

## Commands

The installed owner-only launcher fixes the exact production paths. `plan` is
the only read-only command; the verb itself authorizes the scoped mutation for
`prepare`, `apply`, `accept`, or `rollback`.

```bash
nexpoly-pull-deploy plan --sha <sha> --operation-id <id>
nexpoly-pull-deploy prepare --sha <sha> --operation-id <id>
nexpoly-pull-deploy apply --sha <sha> --operation-id <id>
nexpoly-pull-deploy accept --sha <sha> --operation-id <id>
nexpoly-pull-deploy rollback --operation-id <id>
```

There is one compatibility exception before the first ordinary deployment
from raw manual adoption. With no current-state record, the immutable selector
has no prepared target control release to select, while the installed
`cff408…` planner rejects the adopted MD slot as ungoverned. From the exact
private, standalone, clean, source-pinned target clone used for the one-time
prerequisite and role transactions, run only the read-only plan directly:

```bash
/usr/bin/python3 -I -B ./scripts/pull_deploy_controller.py plan \
  --sha <full-main-sha> \
  --operation-id <id>
```

The target planner accepts raw adoption only when its source, active MD slot,
active control, adoption provenance, and source/CI-matched prerequisite
authority are exact, and it performs no logical writes. Continue with the
installed `nexpoly-pull-deploy prepare`; never run a mutating or recovery verb
from the checkout. Once current-state v3 exists and target controls are active,
subsequent releases use the installed `nexpoly-pull-deploy plan` shown above.

An operation ID cannot be reused for a different SHA. Repeating a completed
phase with the same identity is idempotent; an unknown previous result or an
identity mismatch stops for recovery.

### Historical bridge gate

The following gate describes the retired takeover/bootstrap bridge and is
retained for its recovery record. It is not a prerequisite to rerun on the
current manually adopted production runtime.

Control-plane bootstrap was not immediate production-deploy authorization. The
known duplicate production `0005_polytao_jobs` ledger alias first had to be
reconciled through the content-addressed fixed-purpose maintenance entrypoint,
with its backup and isolated restore evidence. The historical order was:
bootstrap controls; Pull `plan`/`prepare` while production was online; alias
dry-run; maintenance and full source/database-client stop; alias apply with the
same operation ID; then the already prepared Pull `apply`. An incomplete
historical alias marker still fails closed and must be recovered through its
recorded operation; it is never recreated for an ordinary adopted deployment.

## Planning

`plan` is read-only. It validates:

1. the fixed source and runtime roots and their owner-only writable controls;
2. completed manual-adoption and source/CI-matched prerequisite authority;
3. the installed schema-v7 mutable-data helper and private service inputs;
4. a clean production checkout with the expected remote URL;
5. that the requested full SHA is the exact current remote `main`;
6. current deployment and MD/DFT runtime state and absence of a conflicting
   deployment marker.

No service, checkout ref, database, asset pointer or Worker environment changes
during planning. `prepare` subsequently executes the helper and requires the
full schema-v7 role/grant projection before it can seal a descriptor.

## Preparation

`prepare` performs reversible work while the current runtime stays online:

- revalidates the plan under the deployment lock;
- fetches the target, proves it is a fast-forward and validates the single
  shared CI contract: `ci-gate`, immutable image publication and
  `bridge-validation`;
- pulls the two application image digests and records their local identities;
- validates the target asset input, migration policy and production Compose
  files directly from the fetched Git objects;
- reads Worker lock files from the target commit without switching the checkout;
- fills the private, content-addressed wheel cache using hash-locked binary
  distributions;
- chooses only the inactive MD A/B slot and creates its virtual environment at
  the final path;
- builds the DFT virtual environment at
  `worker-venvs/dft/<target-sha>` from target Git blobs and locked inputs,
  without reading or modifying the active DFT runtime;
- verifies the frozen base Python, toolchain, installed distributions and
  absence of source or staging paths in the environment;
- validates the target asset release and records the resolved asset pointer;
- seals the DFT runtime, six model digests, environment, unit and GPU identity
  in descriptor v4;
- writes and fsyncs the private descriptor and ready record.

Preparation never installs from the network after service drain. It does not
overwrite either active Worker runtime. If preparation fails, the serving
runtime and both active Worker identities are unchanged.

After `prepare`, and before `apply`, the source-pinned
`production_postgres_rehearsal.py --plan/--apply` command must produce a sealed
passing report for that exact descriptor/ready pair. It performs a fresh custom
dump, isolated PostgreSQL 16 restore, exact ordered 0014/0015 migration, and
post-migration ledger/property/snapshot/index/query-plan validation. The
backup-plus-restore limit is 30 minutes and the migration limit is 10 minutes.
The migration evidence fixes `lock_timeout=30s` and
`statement_timeout=15min`.
The controller dynamically validates this report with the manifest-sealed
target control release and refuses `apply` if the report is absent, stale, or
has different authority. Exact commands and confirmation fields are in
[deployment.md](deployment.md).

## Apply state machine

`apply` repeats all target and prepared-record checks and validates the sealed
PostgreSQL rehearsal before it records a marker or changes business state. It
then obtains the non-blocking deployment lock and records a crash marker before
the first runtime mutation. Ordinary descriptor-v4 deployment does not reopen
admission inside `apply`. Its durable path continues through two explicit
`accept` calls:

```text
prepared -> rehearsal-bound -> drained -> database-backed-up
         -> source-switched -> runtime-switched -> verified
         -> state-committed -> awaiting-acceptance
         -> acceptance-started -> acceptance-resume-started
         -> admission-resumed
```

The transition sequence is fixed:

1. Consume the exact descriptor/ready-bound rehearsal authority before any
   business mutation.
2. Close public writes and MD/DFT submission, drain the Backend and both
   Workers, and prove both `active=0` and `queued=0` with exact instance/schema
   evidence.
3. Isolate ingress and stop every service that reads checkout files, including
   MD and DFT.
   Persist the exact PostgreSQL container/image/data-volume/system identifier
   before the stop unknown-commit boundary, then prove Worker MainPIDs are zero,
   sockets are absent, and no process reads the live checkout.
4. Create and fsync the drain-final PostgreSQL backup, prove its isolated
   PostgreSQL 16 recovery, and seal that recovery evidence.
5. Record `refs/nexpoly/previous` plus the previous source, images, assets, MD
   slot/unit, and DFT runtime/env/unit identity.
6. Fetch again and reject a changed target or CI status.
7. Fast-forward the production `main` checkout to the exact target. No merge
   commit, rebase, untracked cleanup or operator conflict resolution is allowed.
8. Recompute HEAD and tree identity and reject any worktree difference.
9. Render production Compose from the target checkout using only recorded image
   digests and external runtime paths.
10. Apply exactly the descriptor's ordered 0014/0015 transition, refresh the
    analytics snapshot, and run strict schema preflight.
11. Atomically select the prepared MD and DFT runtimes and their tracked units,
    then start DFT, MD, Backend, and Web/entry in order. PostgreSQL is excluded
    from application `up` calls and its full identity is rechecked around
    startup.
12. Verify the candidate runtime and atomically commit current-state v3. Keep
    ingress and submission closed and leave the marker at
    `awaiting-acceptance`.

PostgreSQL remains running while application services and Workers are stopped.
No process may execute Python, shell or Compose files from the checkout while
Git changes its working tree.

The first `accept --sha <sha> --operation-id <id>` invocation creates a
loopback-only `127.0.0.1:9000` candidate endpoint, runs and seals the internal
DFT/MD/API/UI probes, removes the probe endpoint, and re-drains the candidate.
Only a passing probe report starts the real 900-second maintenance observation
and advances to `acceptance-started`; public admission remains closed. The
probe set covers six-model DFT warmup and a minimum single point, MD one-running
plus two-queued capacity/fourth-submit 429/cancellation, property histogram and
2D structure APIs, knowledge, and the main frontend routes.

Ingress isolation and a candidate-bound `acceptance_probe_intent` are durable
before the first probe. Its full mutable-data digest remains the pre-probe
authority across a failed or crashed attempt that may leave partial MD/DFT
history. The passing path validates a strict pre/post immutable projection:
PostgreSQL/system/role/ledger identity, business schemas, static and analytics
data, migration exceptions, sequence structure, bridge structure and all
non-probe rows must remain unchanged. Only reviewed MD/DFT probe rows, their
dynamic sequences/bridge values and this operation's drain bookkeeping may
change. The resulting post-probe stability digest is sealed in acceptance
evidence.

After the returned `acceptance_not_before`, the operator invokes the same
`accept` command a second time. The controller uses a read-only runtime check,
without submitting another canary, and revalidates the sealed report and
source/current-state/database/runtime/Worker/image identities. Controller-owned
drain timestamp/content refreshes are normalised, but post-probe business and
sequence state remains exact. DFT observe-only `ready`/`quarantined` contention
transitions are dynamic warning state; a valid fresh guard file bound to the
descriptor GPU UUID plus unchanged runtime/process identity is still required.
Only an unchanged candidate advances through `acceptance-resume-started` to
`admission-resumed`, opens ingress and submissions, records the terminal
outcome, and removes the marker. Before `acceptance_resume_intent`, failed
probes or identity drift keep admission closed and require explicit rollback;
staging time before probes never counts toward the 900-second observation. A
stopped runtime is restartable only from `awaiting-acceptance`; at later
acceptance phases it remains stopped and is rejected. After the sticky resume
intent, rollback is forbidden and recovery can proceed forward only. A retry
first read-only validates the exact candidate current state, source, sealed
probe report/authority, and non-mutable database provenance. If persistent
admission and the complete public runtime are already open with the exact
sealed fence, it records `admission-resumed` and terminalizes without isolating
ingress or reading mutable rows. A partial persistent resume is isolated and
exactly re-drained, then compares only candidate, runtime, non-mutable database,
and Worker-fence identities before resuming. Legitimate writes accepted after
the unknown resume commit are never compared with the pre-resume snapshot. A
stopped or drifted runtime retains `acceptance-resume-started` and its sticky
intent and requires a forward fix; it is not converted into a rollbackable
rejection. On both paths, the top-level full runtime-verification and Worker
fence digests must bind the sealed acceptance evidence, and the freshly read
complete repository identity—including Git trust and permission-takeover
evidence—must exactly equal the repository identity sealed by that runtime
verification. A partial re-drain never overwrites this full authority with its
recovery-only observation.

## Runtime and slot records

Private records under the runtime root are the durable authority:

```text
state/deploy.lock
state/deploy-in-progress.json
state/current-deployment.json
state/adopted-deployment.json
state/adopted-prerequisites.json
state/prepared/<operation-id>/descriptor.json
state/prepared/<operation-id>/ready.json
state/prepared/<operation-id>/acceptance-authority.json
state/prepared/<operation-id>/production-acceptance-<operation-id>.json
state/monomer-md-active-slot.json
state/worker-slots/md-a.json
state/worker-slots/md-b.json
config/monomer-dft-runtime.env
audit/deployment-rehearsals/<operation-id>/report.json
```

Each deployment record includes the operation ID, source SHA/tree, remote,
image digests and labels, asset identity, migration identity, active Worker
identities, lock hashes, timestamps, rehearsal authority, and verified
drain-final database backup. MD slot records bind the final venv path, base
Python identity, complete distribution inventory and source SHA. Descriptor v4
additionally binds the DFT venv/runtime manifest, six model digests, env, unit,
and GPU identity. Current-state v3 permanently retains the manual-adoption
provenance.

Files are written through a private temporary file, `fsync`, atomic rename and
parent-directory `fsync`. Symlinks, unexpected ownership, loose permissions,
unknown fields and inconsistent current/previous records fail closed.

## Rollback and recovery

`rollback --operation-id <id>` consumes an in-progress staged attempt. It does
not accept an arbitrary SHA. Before public admission opens, the controller
keeps ingress isolated, stops candidate services, restores the recorded MD
slot/unit and DFT runtime/env/unit as one coherent old-runtime set, restores
previous image and asset identities, and moves the dedicated checkout's local
`main` back to the exact sealed predecessor. It never combines old source with
a candidate Worker environment. The old runtime is admitted only after its
source, database, Worker, image and smoke identities pass.

For the exact 0013→0015 ordinary transition, entering the migration phase is an
unconditional whole-database recovery boundary. Even when a transaction appears
to have failed or rolled back, the controller must restore the verified
drain-final post-0013 dump before starting the old runtime. A failure before
that phase may restore only the sealed source/images/assets/Workers. A failed
probe, observation drift, crash at an acceptance boundary, or ambiguous record
never authorizes public admission; explicit rollback keeps the fence closed
until recovery is proven.

After a descriptor-v4 deployment has completed acceptance and reopened public
admission, ordinary `rollback` fails before it writes a recovery marker, takes
a backup, drains, or stops any service. The supported response is a forward
fix. Restoring the drain-final post-0013 dump at that point would discard every
write accepted after release, while retaining the post-0015 database would put
the old source on an unsupported schema. Such a data-loss rollback therefore
requires a separately reviewed and authorized maintenance entrypoint; the
ordinary release controller deliberately has no override flag.

The same fence begins before the final `resume` call. The controller first
persists an `acceptance_resume_intent` bound to the operation and candidate
state. `acceptance-resume-started` is therefore an unknown public-admission
commit boundary, not a staged rollback phase. New recovery attempts preserve
that phase on stopped-runtime or identity failure. A pre-existing marker that
already combines the sticky intent with `acceptance-rejected` remains valid,
but automatic convergence and rollback both refuse it; a separately reviewed
forward fix is required. No path restores the database after this intent.

A staged rollback has a separate sticky boundary. After the previous
database/effects/current state are restored, but before old admission opens,
the marker records `rollback_admission_resume_intent` bound to the failed
candidate, previous deployment or adoption authority, backup/restore evidence,
and runtime recovery fence. Resume success advances to
`rollback-admission-resumed`. Recovery from either phase may verify a fully
open exact runtime or re-drain and resume it forward, but cannot repeat the
database restore or effect rollback; this preserves writes accepted after a
lost resume response.

On process death, the marker phase determines recovery. A missing or ambiguous
record, changed Git tree, changed slot, missing image, unverified backup or
unowned database causes admission to stay closed. Operators must not remove
markers, hand-edit records or the migration ledger, or start services outside
the controller.

## MD capacity and DFT guard policy

MD has two separate limits. `MONOMER_MD_MAX_ACTIVE_JOBS=3` is the total number
of non-terminal jobs admitted across running and queued states;
`MONOMER_MD_MAX_CONCURRENT_JOBS=1` is the execution limit. The production
contract is therefore exactly one running plus two queued, with the fourth
active submission returning 429. A value of three for `MAX_ACTIVE_JOBS` does
not authorize three concurrent GPU executions.

DFT remains one running plus eight queued on physical GPU2, with Broker, MPS,
and overflow disabled. Production explicitly pins
`NEXPOLY_DFT_GPU_GUARD_MODE=observe`; the general default remains `enforce`.
Observe mode continues the GPU2 scan and exposes only the structured
`gpu_guard_mode`, `gpu_guard_status`, and `gpu_contention_observed` projections.
An unknown external GPU2 process may produce `quarantined` and a warning, but
does not make an otherwise healthy runtime unavailable and does not block
startup, task submission, or execution. The controller neither kills nor
allowlists that process. Initial deployment readiness still requires a current,
well-formed observation with the sealed GPU UUID and accepts either `ready` or
`quarantined`; later missing/stale/invalid observations warn without closing
service.

Observe mode is explicitly best-effort: GPU contention can still cause OOM,
timeout, or CUDA failures. Acceptance nevertheless requires six-model warmup
and a minimum DFT single-point result while the maintenance fence remains
closed. No status or capability response publishes process PID, user name, or
command line.

## Contract migrations

Normal `apply` executes only the migration mode allowed by the current schema
epoch. It stops before an unapproved trailing contract.

The checksum-pinned `0012_drop_polytao_jobs` operation remains a separate
maintenance command. The retired bundle controller deliberately does not expose
it. `runtime/bin` contains only the immutable selector and the stable deploy,
0012, and fixed-purpose 0005-alias wrappers. The selector loads
`pull_contract_0012.py` and its byte-identical, CLI-retired governance core from
the active content-addressed control release. The adapter binds the successful
pull deployment record, sealed prepared descriptor, clean live SHA/tree and
canonical manifest before it can inspect or mutate the database. It requires a
global drain, registered-database inventory, a full and table-level archive,
PostgreSQL 16 restore proof, exact SQL checksum, approval journal, epoch barrier
and rollback floor. See
`docs/postgres-migration-governance.md` for the complete contract procedure and
operator commands.

## Security invariants

- The source checkout and Git metadata are not group- or world-writable.
- Runtime configuration, records, wheels and backups are private to the deploy
  user.
- Secrets are never command arguments, Git content, JSON audit values or log
  output.
- Production Compose contains no application build context and no mutable
  application image reference.
- The controller never performs `git clean`, rebase, an ordinary merge or an
  unrestricted reset.
- Its controlled previous-SHA checkout is hard-locked to the dedicated
  production repository and cannot target development or DFT worktrees.
- GPU Broker/MPS activation and DFT admission remain separately authorized
  operations.

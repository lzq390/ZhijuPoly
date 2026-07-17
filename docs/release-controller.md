# NexPoly pull-deployment controller

The production controller applies one reviewed `origin/main` commit to the
dedicated production checkout. It is installed under the external runtime root
so replacing checkout files cannot replace the process that is coordinating the
deployment.

## Fixed identities

```text
source checkout  /data/lzq/gith/nexpoly
runtime root     /data/lzq/gith/nexpoly-runtime
controller       /data/lzq/gith/nexpoly-runtime/bin/nexpoly-pull-deploy
alias repair     /data/lzq/gith/nexpoly-runtime/bin/nexpoly-reconcile-production-0005-polytao-alias
bridge recovery  /data/lzq/gith/nexpoly-runtime/legacy-takeover/bin/nexpoly-bridge-recover
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

## Commands

The installed owner-only launcher fixes the exact production paths. `plan` is
the only read-only command; the verb itself authorizes the scoped mutation for
`prepare`, `apply`, or `rollback`.

```bash
nexpoly-pull-deploy plan --sha <sha> --operation-id <id>
nexpoly-pull-deploy prepare --sha <sha> --operation-id <id>
nexpoly-pull-deploy apply --sha <sha> --operation-id <id>
nexpoly-pull-deploy rollback --operation-id <id>
```

An operation ID cannot be reused for a different SHA. Repeating a completed
phase with the same identity is idempotent; an unknown previous result or an
identity mismatch stops for recovery.

Control-plane bootstrap is not immediate production-deploy authorization. The
known duplicate production `0005_polytao_jobs` ledger alias must first be
reconciled through the content-addressed fixed-purpose maintenance entrypoint,
with its backup and isolated restore evidence. The required order is bootstrap
controls; run Pull `plan`/`prepare` while production is online; run the alias
dry-run; enter maintenance and stop every source/database client; apply the
alias with the same operation ID; then run the already prepared Pull `apply`.
Before that gate, do not invoke production Pull `apply` or `bootstrap-expand`.
If an alias marker is incomplete, even `plan`/`prepare` remain blocked until the
recorded alias control recovers it.

## Planning

`plan` is read-only. It validates:

1. the fixed source and runtime roots and their owner-only writable controls;
2. a clean production checkout with the expected remote URL;
3. that the requested full SHA is the exact current remote `main`;
4. current deployment and Worker-slot state and absence of a conflicting
   deployment marker.

No service, checkout ref, database, asset pointer or Worker environment changes
during planning.

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
- chooses only the inactive A/B slot and creates its virtual environment at the
  final path;
- verifies the frozen base Python, toolchain, installed distributions and
  absence of source or staging paths in the environment;
- validates the target asset release and records the resolved asset pointer;
- writes and fsyncs a private prepared record.

Preparation never installs from the network after service drain. It does not
overwrite the active slot. If preparation fails, the serving runtime and its
active slot are unchanged.

## Apply state machine

`apply` repeats all target and prepared-record checks, obtains the non-blocking
deployment lock, and records a crash marker before the first runtime mutation.
The durable phases are:

```text
prepared -> drained -> database-backed-up -> source-switched
         -> runtime-switched -> verified -> committed
```

The transition sequence is fixed:

1. Drain the Backend and each enabled Worker, recording instance IDs and exact
   active-job schema evidence.
2. Isolate ingress and stop every service that reads checkout files.
   Persist the exact PostgreSQL container/image/data-volume/system identifier
   before the stop unknown-commit boundary.
3. Back up PostgreSQL, fsync the dump and sidecar, restore it into isolated
   PostgreSQL 16, and verify its schema and row digests.
4. Record `refs/nexpoly/previous` and the previous deployment identity.
5. Fetch again and reject a changed target or CI status.
6. Fast-forward the production `main` checkout to the exact target. No merge
   commit, rebase, untracked cleanup or operator conflict resolution is allowed.
7. Recompute HEAD and tree identity and reject any worktree difference.
8. Render production Compose from the target checkout using only recorded image
   digests and external runtime paths.
9. Apply the permitted migration mode, refresh the analytics snapshot and run
   strict schema preflight.
10. Atomically select prepared Worker slots, then start Worker and Backend with
    PostgreSQL excluded by `--no-deps`; revalidate the same PostgreSQL identity
    before and after startup, then start Web in dependency order.
11. Verify required model preload, Worker identity, database health, isolated
    API/UI/calculation smokes and final public health.
12. Atomically commit current/previous state, remove the marker and resume
    admission.

PostgreSQL remains running while application services and Workers are stopped.
No process may execute Python, shell or Compose files from the checkout while
Git changes its working tree.

## Runtime and slot records

Private JSON records under `nexpoly-runtime/state` are the durable authority:

```text
deploy.lock
deploy-in-progress.json
current-deployment.json
prepared/<operation-id>/descriptor.json
prepared/<operation-id>/ready.json
monomer-md-active-slot.json
worker-slots/md-a.json
worker-slots/md-b.json
```

Each deployment record includes the operation ID, source SHA/tree, remote,
image digests and labels, asset identity, migration identity, active Worker
slots, lock hashes, timestamps and the verified database backup. Slot records
bind the final venv path, base Python identity, complete distribution inventory
and the source SHA for which the slot was prepared.

Files are written through a private temporary file, `fsync`, atomic rename and
parent-directory `fsync`. Symlinks, unexpected ownership, loose permissions,
unknown fields and inconsistent current/previous records fail closed.

## Rollback and recovery

`rollback --operation-id <id>` consumes the recorded attempt. It does not accept
an arbitrary SHA. The controller keeps ingress isolated, stops candidate
services, selects the recorded previous Worker slots and images, restores the
previous asset pointer, and moves the dedicated checkout's local `main` back to
the exact commit protected by `refs/nexpoly/previous`. The old runtime is
admitted only after its identity and smokes pass. A later deployment fetches and
performs the normal fast-forward checks from that clean local `main`.

If a database or data transformation crossed a compatibility boundary, rollback
must restore the verified dump before the old runtime accepts writes. Compatible
expand migrations may remain only when the previous schema floor explicitly
allows them.

On process death, the marker phase determines recovery. A missing or ambiguous
record, changed Git tree, changed slot, missing image, unverified backup or
unowned database causes admission to stay closed. Operators must not remove
markers, hand-edit records or the migration ledger, or start services outside
the controller.

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

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

That prerequisite authority is followed by the dedicated adopted-checkout
permission transaction from the same exact private source. It is deliberately
not the historical installer or a raw permission primitive:

```bash
permission_operation_id=adopt-git-permission-<utc-timestamp>

./scripts/adopt_runtime_prerequisites.py permission-plan \
  --sha <full-main-sha> \
  --operation-id "$permission_operation_id"

./scripts/adopt_runtime_prerequisites.py permission-apply \
  --sha <full-main-sha> \
  --operation-id "$permission_operation_id" \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \
  --confirm-permission-impact-sha256 sha256:<reviewed-impact-digest>
```

`<utc-timestamp>` is a lowercase, colon-free slug such as
`20260814t140705z`, matching the operation-ID grammar. Exact `HEAD`, moving
`origin/main`, and protected-main CI are checked for the initial plan and first
durable `intent`. Once intent exists, same-operation replay/abort uses the
sealed source plus the two confirmed digests and does not re-query moving main
or CI authority, so recovery remains possible after a later main advance.

`permission-plan` is logically zero-write and binds the complete permission
inventory. `plan_sha256` confirms the full source/authority/permission plan;
`permission_impact_sha256` independently confirms the exact inode mode
transitions. The current production observation reports 167 transitions, but
that number is not a policy constant: only the records, count, and digest in
the fresh reviewed plan are authoritative.

The apply uses the shared `state/deploy.lock` and publishes
`state/adopted-git-permissions.json` only after its source and raw adoption,
bootstrap, prerequisite, and permission evidence still compare exactly. Its
only non-authority mutation is the exact planned checkout root and `.git/**`
metadata mode set; ordinary working-tree files, source content/refs,
PostgreSQL, containers, credentials, and services remain unchanged. Its
authority kind is
`manual-runtime-adoption-permission-hardening`. The private journal at
`state/adopted-git-permission-transactions/<operation-id>.json` advances
`intent` → `permission-change-intent` → `permission-ready` →
`source-verified` → `authority-commit-intent` → `completed`. Before the
durable permission-change intent, the same operation may be aborted with both
confirmations:

```bash
./scripts/adopt_runtime_prerequisites.py permission-abort \
  --sha <full-main-sha> \
  --operation-id "$permission_operation_id" \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \
  --confirm-permission-impact-sha256 sha256:<reviewed-impact-digest>
```

Marker generations are durable authority. The current marker retains its
immediate predecessor and every older lifecycle generation under a
generation-and-digest-addressed `.retired-g*` name; the transaction never
unlinks those records. Every replay validates a bounded, contiguous chain.
Before any marker write or permission change, cumulative history is limited to
512 MiB and the filesystem must also retain a 64 MiB free-space margin. These
predecessor and retired files must not be cleaned up operationally.

At or after `permission-change-intent`, partial mode changes are an unknown
commit and abort is forbidden. Recovery is forward-only: only the same
`permission-apply`, operation ID, SHA, and two digests may converge. The
completed authority seals both source identities, the raw
adoption/bootstrap/prerequisite digests, both confirmation digests, the full
plan, and the raw marker/evidence/inventory/original/hardened permission
digests. It is immutable.

The permission helper does not invoke the old installed controller as a probe.
Instead it deliberately produces the unchanged schema-v1 hardened marker at
`state/legacy-git-permission-takeover.json`, which is compatible with that
controller's generic verifier. The new target controller independently
requires that marker and the adopted wrapper authority to bind each other and
seals the compact permission projection together with the unit-permission
authority in the historical schema-v3 prerequisite target binding.
Schema-v2/schema-v3 remain read compatibility for historical descriptor
provenance only; a new source-successor raw-adoption descriptor uses schema-v4
and cannot omit any authority layer. The controller repeats the full check
during formal plan, prepare/resume, and apply pre-switch. Do not invoke
`git_source_trust.takeover_repository_permissions` directly, run
`install_legacy_takeover_prerequisites.py`, synthesize the marker, or use
manual `chmod`; none of those paths creates this authority.

The first-deployment authority chain is executable only in this order:
snapshot → source-successor → unit-permission → bootstrap-router. These commands are production-authorized only for the single frozen target whose
protected-main CI is complete. Do not start any mutating step until the immediately preceding authority is completed and its raw digest has been
independently reviewed. Neither an operator waiver nor a manually copied
`.git` directory satisfies this gate.

The follow-up must cover every first-deployment Git mutation, from the first
fetch through source switch and rollback. Any failed path that
returns to the predecessor or begins a new operation must
restore the entire verified pre-prepare `.git` snapshot before proceeding.
Resetting `HEAD` or deleting individual refs is not sufficient to re-establish
the sealed B/F/P state.

Freeze protected `main` before starting the snapshot `plan`, and keep
it frozen—with no merge, push, force-update, or branch automation—
until the first deployment has durably written current-state v3. If `main`
advances before any durable successor intent, discard the reviewed plan;
discard every uncommitted reviewed plan whenever protected `main` changes and
create a fresh chain for the new tip. If a transaction is still abortable, use
its confirmed abort before replanning. Once
`authority-commit-intent` or the create-once authority is `completed`, any
`main` advance is a permanent stop for this first-deployment path:
do not publish a second successor authority or change the sealed target. Use a
separately reviewed compatibility-recovery procedure.

First create the independent whole-directory snapshot authority:

```bash
snapshot_operation_id=snapshot-git-<utc-timestamp>

./scripts/production_git_snapshot.py plan \
  --sha <full-main-sha> \
  --operation-id "$snapshot_operation_id"

./scripts/production_git_snapshot.py apply \
  --sha <full-main-sha> \
  --operation-id "$snapshot_operation_id" \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \
  --confirm-snapshot-impact-sha256 sha256:<reviewed-impact-digest>
```

The plan is logically zero-write. Apply publishes the create-once
`state/production-git-snapshot.json` only after an independent no-link/no-
reflink copy, manifest re-scan, and strict `git fsck`; it changes no live Git
byte, unit, process, container, service, database, or credential.

If the final reviewed first-deployment target changes a blob governed by that
immutable root, publish the fixed source-successor authority with the separate
standard-library-only tool before hardening the Worker unit:

```bash
source_successor_operation_id=adopt-git-successor-<utc-timestamp>

./scripts/adopt_git_permission_source_successor.py plan \
  --sha <full-main-sha> \
  --operation-id "$source_successor_operation_id"

./scripts/adopt_git_permission_source_successor.py apply \
  --sha <full-main-sha> \
  --operation-id "$source_successor_operation_id" \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \
  --confirm-source-successor-impact-sha256 sha256:<reviewed-impact-digest>
```

The candidate bootstrap, Git-trust, and bridge CI-contract files are data, not
publisher code. Only the frozen bootstrap and Git-trust verifiers from the old
root authority execute; their required CI jobs are literal-sealed before the
target check. The target `scripts/bridge_deploy_core.py` blob must remain
byte-identical. The fixed 13-file manifest contains exactly the ten
prerequisite install source paths plus `scripts/bootstrap_pull_deploy.py`,
`scripts/git_source_trust.py`, and `scripts/bridge_deploy_core.py`; it is never
populated by discovery. It seals Git type/mode/blob/content identity for every
record, and the changed set must be exactly
`scripts/bootstrap_pull_deploy.py` plus `scripts/git_source_trust.py`.

The old completed journal's `source_trust_sha256` and the newly observed
`production_source_trust_sha256` become
`predecessor_source_trust_sha256` and `production_source_trust_sha256` in the
successor plan; they are separate evidence and must not be compared for
equality. Each must match its own exact evidence, even when a post-adoption
remote tracking ref changed without changing production HEAD, tree, or
worktree content.

The successor authority also seals the only permitted repository transition.
`B` is the adopted baseline: local `main` and
`refs/remotes/nexpoly-deploy/main` are direct commit refs, the deploy ref names
the predecessor authority, and no prepared ref exists. `F` is the fully
materialized target with the deploy ref at the exact target and no prepared
ref. `P(operation)` is `F` plus exactly
`refs/nexpoly/prepared/<operation>=<target>`. A first plan accepts only `B` or
`F`; target prepare, resume, and pre-switch validation accept only the current
`P(operation)`. An abort may leave the harmless empty `refs/nexpoly` namespace,
so a later plan may resume from `F`, but it may not retain another operation's
prepared ref.

The authority binds the stable checkout/config/index projection, exact
baseline logical and raw refs, baseline auxiliary Git metadata, all baseline
semantic objects, the exact target-reachable closure, and their expected set
union. Post-fetch verification requires that semantic union exactly—neither a
missing baseline dangling object nor an injected valid object is accepted.
Physical object storage is limited to canonical loose objects, pack/index/rev
sidecars, and supported commit-graph paths. Any `.lock`, `tmp_pack_*`, partial
pack sidecar, noncanonical object path, packed-ref drift, symref substitution,
or unexpected auxiliary file fails closed. `FETCH_HEAD` must be the one exact
protected-main SSH fetch record. An existing deploy reflog must retain its
exact sealed prefix; at most the exact single fast-forward append is accepted.
The descriptor then seals the complete post-fetch trust, semantic, physical
object-storage, logical/raw-ref, and auxiliary digests, and every resume or
pre-switch proof must reproduce them byte-for-byte.

A network failure may be retried with the same operation only when the
repository proves as clean `B`, `F`, or the current `P(operation)` and no lock
or temporary object remains. A host power loss during the old controller's
fetch has a stricter stop condition: a broken ref, failed strict `fsck`, lock,
temporary pack, truncated `FETCH_HEAD`, or non-prefix reflog change is not
self-healed by another prepare, because the old controller rechecks repository
identity before refetching. Do not delete individual files or rewrite refs by
hand. Keep traffic unchanged, stop the release, and restore the entire
`.git` directory from a separately verified pre-prepare owner-private snapshot
or use a separately reviewed recovery procedure; then re-run plan/prepare with
a new operation ID unless the original operation is independently proved to be
the exact surviving `P(operation)`.

After a terminal decision abandons the interrupted operation, the only normal
restore path is the dedicated whole-directory transaction:

```bash
restore_operation_id=restore-git-<utc-timestamp>

./scripts/restore_production_git_snapshot.py plan \
  --restore-operation-id "$restore_operation_id" \
  --abandoned-deploy-operation-id <abandoned-deploy-operation-id> \
  --terminal-decision restore-before-new-operation

./scripts/restore_production_git_snapshot.py apply \
  --restore-operation-id "$restore_operation_id" \
  --abandoned-deploy-operation-id <abandoned-deploy-operation-id> \
  --terminal-decision restore-before-new-operation \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \
  --confirm-snapshot-authority-sha256 \
    sha256:<reviewed-snapshot-authority-digest>
```

It atomically exchanges all of `.git`, archives the displaced directory, and
materializes a clean worktree without ever turning the golden copy into the
live directory.

The zero-write plan and separately confirmed source-successor impact bind the
old raw authority/marker, adoption/bootstrap/prerequisite bytes, target
SHA/tree, ancestry, full workflow run/attempt, fixed manifest, and create-once
namespace. Apply writes only
`state/adopted-git-permission-source-successor-transactions/<operation-id>.json`
and the fixed mode-`0600`
`state/adopted-git-permission-source-successor.json`; it does not alter source,
permissions, units, services, containers, PostgreSQL, or credentials. Its
journal advances `intent` → `predecessor-verified` → `source-verified` →
`authority-commit-intent` → `completed`. Commit intent is forward-only. Before
that boundary, abort requires the same two confirmations:

```bash
./scripts/adopt_git_permission_source_successor.py abort \
  --sha <full-main-sha> \
  --operation-id "$source_successor_operation_id" \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \
  --confirm-source-successor-impact-sha256 sha256:<reviewed-impact-digest>
```

The controller permanently validates the complete old-root → source-successor
→ unit-authority chain. Missing, malformed, partial, or changed successor
lineage is fail-closed and never falls back to schema-v1 byte identity.

The last one-time predecessor transaction hardens the adopted Worker unit:

The Worker-unit command remains prohibited until the snapshot and source-successor authorities are complete.

```bash
unit_permission_operation_id=adopt-unit-permission-<utc-timestamp>

./scripts/adopt_runtime_prerequisites.py unit-permission-plan \
  --sha <full-main-sha> \
  --operation-id "$unit_permission_operation_id"

./scripts/adopt_runtime_prerequisites.py unit-permission-apply \
  --sha <full-main-sha> \
  --operation-id "$unit_permission_operation_id" \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \
  --confirm-unit-permission-impact-sha256 sha256:<reviewed-impact-digest>
```

Its logically zero-write plan seals full MD/DFT unit, parent-directory,
systemd, PID, and InvocationID evidence plus the raw predecessor-authority
digests. It reports `atime_zero_write=true` only when every source, runtime,
production, and unit-parent mount is read-only or suppresses atime; otherwise
the read-only inventory may update atime and is not a physically zero-write
observation. The parent
directory's identity, ownership, mode, and link count remain exact; only its
reported size may grow as a filesystem allocation side effect of this
transaction's own temporary entry. A completed source-successor authority must
target this unit transaction's exact source SHA/tree. The plan independently
binds both old and new raw authorities. Apply atomically exchanges a newly
created `0600` MD unit
inode for the legacy `0664` inode, keeps a private operation-owned backup, and
treats the already-`0600` DFT unit as an exact no-op CAS. It invokes only a
user-manager daemon reload: neither Worker is stopped or restarted, and both
running identities plus `NeedDaemonReload=no` must remain exact.

The plan and impact also seal an `authority_publication` ownership record for
the exact runtime `state` directory and three operation-bound names:
`adopted-unit-permissions.json`, its deterministic
`.adopted-unit-permissions.json.create-<operation-id>` staging name, and the
matching `.quarantine` name, each with `initially_absent=true`. Apply proves
that namespace durably absent through the pinned state-directory descriptor
before first intent and again immediately before the MD inode exchange. Only
durable `authority-commit-intent` may recover an expected-payload staging,
quarantine, or linked-final residue; `completed` requires the exact
single-link final authority and no residue. A same-operation weak authority,
unowned hard link, preplant, or pathname/inode swap fails closed before any
new MD replacement or daemon reload.

Abort is allowed only before durable `replacement-intent`, using
`unit-permission-abort` and both confirmations. Every later crash is
forward-only replay of the same apply. The immutable
`state/adopted-unit-permissions.json` embeds complete original, hardened, and
backup evidence and their recomputable digests. The only tolerated prepared
residue is an exactly validated `operation-archive-intent` prepare-abort with
no descriptor/ready authority and no live ref or handoff; every other prepared
state remains fail-closed.

For this source-successor-bearing raw adoption, the unit plan, journal, and
final `state/adopted-unit-permissions.json` authority must all use schema v2;
schema v1 remains historical read compatibility only. The resulting raw
adoption must emit descriptor v4 and, after a successful deployment,
current-state v3. The outer descriptor v4 contains a schema-v5 adopted-
prerequisite target binding that consumes the raw old-root, source-successor,
completed source journal, schema-v2 Worker-unit authority, completed unit
journal, complete unit transaction inventory, and snapshot/router anchors.
Historical inner binding schemas v2-v4 are read compatibility only. The
inventory must contain exactly one completed journal; any other entries must
be valid terminal no-mutation aborts. A projected authority, a schema-v1 unit
authority, a nonterminal journal, or an unsealed transaction entry cannot
satisfy that chain.

Current-state v3 treats `adoption_successor_lineage` as a permanent
eight-anchor schema-v3 record: `source_successor_authority_sha256`,
`source_successor_completed_journal_sha256`, and
`unit_permission_authority_sha256`,
`unit_permission_completed_journal_sha256`, and
`unit_permission_transaction_inventory_sha256`, plus
`production_git_snapshot_authority_sha256`,
`bootstrap_router_intent_sha256`, and
`bootstrap_router_authority_sha256`. Historical current-state v3 records may
omit this record or carry the legacy schema-v1 three-anchor or schema-v2 five-
anchor form; an authorized successor binding preserves all existing values
while upgrading the record. After that upgrade, once written, all eight
anchors are permanent: every later current-state v3 must preserve their exact
values, and deletion or replacement fails closed.

Publish the one-time bootstrap router only after the unit authority completes:

```bash
router_operation_id=adopt-router-<utc-timestamp>

./scripts/adopt_bootstrap_router_successor.py plan \
  --sha <full-main-sha> \
  --operation-id "$router_operation_id"

./scripts/adopt_bootstrap_router_successor.py apply \
  --sha <full-main-sha> \
  --operation-id "$router_operation_id" \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \
  --confirm-router-successor-impact-sha256 sha256:<reviewed-impact-digest> \
  --confirm-snapshot-authority-sha256 sha256:<reviewed-snapshot-authority-digest> \
  --confirm-source-successor-authority-sha256 sha256:<reviewed-source-authority-digest> \
  --confirm-unit-permission-authority-sha256 sha256:<reviewed-unit-authority-digest> \
  --confirm-predecessor-selector-sha256 sha256:<reviewed-predecessor-selector-digest>
```

It installs the content-addressed target controls and snapshot verifier while
leaving `active-control`, Worker routes, processes, units, Git, and PostgreSQL
unchanged. The durable selector-swap intent is
`state/bootstrap-router-successor-intent.json`; the create-once completion is
`state/bootstrap-router-successor.json` with authority kind
`manual-runtime-adoption-bootstrap-router-successor`. The transaction keeps a
temporary `state/contract-0012-in-progress.json` fence until the new selector
and completion authority are both durable. The selector rejects the direct
first-deployment `plan`, runs snapshot `verify-integrity` before `prepare` can
perform the first Git write, and retires the one-time target route only after
target-bound current-state v3 and the exact T active-control both exist. A
merely present or forged current-state fails closed.

The same exact source then runs
`provision_mutable_data_audit_role.py --plan/--apply`. Formal Pull `plan` and
`prepare` are forbidden until the prerequisite, permission, and role
transactions all complete. The resulting mutable-data audit helper report is
schema-v7 and proves an explicit
least-privilege projection: no elevated role attributes, memberships,
ownership, cluster-wide predefined read role, write authority, or authority
outside the governed schemas. The only normalized direct grants are
database `CONNECT`, governed schema `USAGE`, current table/view and sequence
`SELECT`, and `polyprop`'s schema-scoped future `SELECT` defaults; direct
function grants and security-definer execution are absent. Apply separately
confirms the reviewed impact of revoking PUBLIC execution from the eight
large-object mutators. No secret or plaintext password is a command argument
or audit value.

The ordinary deployment commands remain prohibited until the bootstrap-router authority is complete and binds the exact required snapshot authority.

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
from raw manual adoption. The bootstrap-router authority has installed the
target control release, but that minimal release intentionally has no complete
target Git object database and therefore cannot prove the strict predecessor-
to-target ancestry and blob relation required by raw adoption. The stable
selector explicitly rejects this first `plan`; it neither falls back to A nor
attempts a partial proof. From the exact private, standalone, clean, complete-
history, source-pinned target clone used for the one-time authority and role
transactions, run only the read-only plan directly:

```bash
deploy_operation_id=deploy-<utc-timestamp>

/usr/bin/python3 -I -B ./scripts/pull_deploy_controller.py plan \
  --sha <full-main-sha> \
  --operation-id "$deploy_operation_id"
```

The target planner accepts raw adoption only when the active MD slot, active
control, adoption provenance, completed prerequisite authority, adopted Git
permission wrapper, and its hardened marker are exact, and it performs no
logical writes. The prerequisite and permission authorities remain immutable
records of the reviewed source and evidence at which they were created; they
are never rewritten to impersonate a newer target.

That authority may bind either to the exact same target source or to a strict
successor under the `ancestor-byte-identical` compatibility mode. The latter is
accepted only when the authority SHA is an ancestor of the exact remote-main
target, and every fixed prerequisite source blob is present at both commits,
byte-identical, and equal to its sealed authority digest. The direct plan proves
this from the exact private target clone and emits a deterministic
`adopted_prerequisite_target_binding` containing the mode, authority and target
SHA/tree, blob-inventory digest, sealed authority readiness digest, and target
source-trust/readiness projection digest. For a new source-successor raw
adoption this becomes a schema-v5 binding containing the immutable root Git,
source-successor, unit-permission, snapshot, and bootstrap-router authorities
with their exact raw evidence digests. Schemas v2-v4 are accepted only when
revalidating historical descriptor provenance; none is emitted for a new raw-
adoption deployment. A descriptor-v4 record
that contains `adopted_deployment` must contain this binding; a descriptor
without adopted deployment must not contain it.

This conditional descriptor-v4 tightening is valid only for the first raw
adoption when no current-state record, deployment marker, or pre-existing
prepared descriptor exists. The current production installation has been
proved read-only to satisfy those conditions. If any such state or an older
adopted descriptor is present, stop for explicit compatibility handling; do
not infer, retrofit, or silently omit the binding.

Continue with the installed `nexpoly-pull-deploy prepare`; never run a mutating
or recovery verb from the checkout:

```bash
nexpoly-pull-deploy prepare \
  --sha <full-main-sha> \
  --operation-id "$deploy_operation_id"
```

Before the old active controller can fetch, the selector verifies the golden
snapshot byte-for-byte and routes to the sealed T controller. `prepare` then
independently reproves the ancestry and every old/target blob from the strictly
trusted production repository after fetching the target. It also reads the
adopted permission wrapper, schema-compatible marker, and all eight raw lineage
anchors from production and recomputes their digests. Resume, already-ready
replay, and `apply` pre-switch validation repeat the proof and exact binding
comparison, so authority, permission, ancestry, blob, snapshot, or router drift
fails before source switching. The old delivery gate remains exact for the
authority SHA, while the target's current remote-main and CI evidence remain
exact for the target SHA. Once current-state v3 exists and target controls are
active, subsequent releases use the installed `nexpoly-pull-deploy plan` shown
above.

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
[deployment.md](deployment.md). The fixed ordinary sequence next runs the
read-only rehearsal plan and its separately confirmed apply:

```bash
./scripts/production_postgres_rehearsal.py \
  --sha <full-main-sha> \
  --operation-id "$deploy_operation_id" \
  --plan

./scripts/production_postgres_rehearsal.py \
  --sha <full-main-sha> \
  --operation-id "$deploy_operation_id" \
  --apply \
  --confirm-descriptor-sha256 sha256:<reviewed-descriptor-digest> \
  --confirm-source-system-identifier <reviewed-decimal-system-identifier> \
  --confirm-source-ledger-sha256 sha256:<reviewed-ledger-digest> \
  --confirm-source-property-records 615159 \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest>
```

## Apply state machine

`apply` repeats all target and prepared-record checks and validates the sealed
PostgreSQL rehearsal before it records a marker or changes business state. It
then obtains the non-blocking deployment lock and records a crash marker before
the first runtime mutation. Ordinary descriptor-v4 deployment does not reopen
admission inside `apply`. Its durable path continues through two explicit
`accept` calls:

```bash
nexpoly-pull-deploy apply \
  --sha <full-main-sha> \
  --operation-id "$deploy_operation_id"
```

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
state/adopted-git-permissions.json
state/adopted-unit-permissions.json
state/legacy-git-permission-takeover.json
state/adopted-git-permission-transactions/<operation-id>.json
state/adopted-unit-permission-transactions/<operation-id>.json
state/adopted-unit-permission-backups/.<operation-id>.owner.json
state/adopted-unit-permission-backups/<operation-id>/.owner.json
state/adopted-unit-permission-backups/<operation-id>/nexpoly-monomer-md-worker.service
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
provenance; its immutable descriptor retains the adopted Git permission
binding.

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

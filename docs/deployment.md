# NexPoly production deployment

Production is updated by a reviewed, commit-pinned pull on the server. GitHub
Actions validates every candidate and publishes the Backend and Web images, but
it never contacts or mutates the production host. An operator runs the stable
deployment command during an authorized maintenance window.

The final read-only F/B admission report and its strict evidence contract are
documented in [production-readiness.md](production-readiness.md).

## Filesystem boundaries

The source checkout is fixed at:

```text
/data/lzq/gith/nexpoly
```

It must track `origin/main` and be completely clean: no tracked changes, no
ordinary untracked paths, and no ignored paths. The current production
installation has already sealed its original checkout through manual adoption.
The older takeover path, retained below only for historical recovery context,
required every ignored path to be inventoried by hash/owner/mode and moved only
by the source-pinned legacy controller. Never use `git clean` or manually move
a path to satisfy either authority.
Runtime state is never stored in that checkout:

```text
/data/lzq/gith/nexpoly-runtime/
├── bin/
├── config/
├── state/
├── audit/
├── backups/
├── wheel-cache/
└── worker-venvs/
    ├── md-a/
    ├── md-b/
    └── dft/
        └── <release-sha>/
```

The runtime root, configuration, state, audit data, backups, cached wheels and
Worker environments are deploy-user-owned and private. Model and database
assets remain in their content-addressed asset store; only the reviewed pointer
under `nexpoly-runtime/state/current-assets` is used by production.

PostgreSQL data, Worker journals, sockets, caches and calculation results are
outside Git. A source update must not copy, delete or recreate them.

## CI contract

The single `.github/workflows/ci.yml` runs for pull requests and pushes to
`main`.

- Pull requests run policy, Backend, Frontend, Worker and image-build checks.
- A push to `main` builds and publishes exactly two images tagged with the full
  commit SHA.
- Both images carry `revision`, `source` and `version` OCI labels.
- CI resolves the pushed tags to immutable digests and smokes those exact
  digests against PostgreSQL 16.
- The production-alias integration remains an independent PG16 restore test;
  a separate real-Docker matrix pulls the exact PG14/15/16/18 audit digests,
  exercises matching-major dormant-volume isolation, and verifies the PG18
  `/var/lib/postgresql/18/docker` source layout.
- The named `bridge-validation` job is part of the same required-job contract
  consumed by bootstrap, Pull and the F→B bridge policy.
- CI has no production environment, host credentials or production execution
  step.

Production always uses the digest references resolved from the requested SHA;
it never uses `latest` and never builds application images on the server.

## Production configuration

Install the examples as private files:

```text
/data/lzq/gith/nexpoly-runtime/config/deploy.env
/data/lzq/gith/nexpoly-runtime/config/app.env
/data/lzq/gith/nexpoly-runtime/config/worker.env
```

`deploy.env` must define:

```dotenv
NEXPOLY_RUNTIME_ROOT=/data/lzq/gith/nexpoly-runtime
NEXPOLY_APP_ENV_FILE=/data/lzq/gith/nexpoly-runtime/config/app.env
NEXPOLY_GPU_STATE_ROOT=/data/lzq/gith/nexpoly-runtime/state/gpu-resource
NEXPOLY_ASSET_ROOT=/data/lzq/gith/nexpoly-runtime/state/current-assets
```

Secret values are read from private files or the environment. They are never
accepted as command-line arguments or recorded in audit JSON.

The production Git remote uses a dedicated read-only deploy identity and a
pinned host key. Personal credential helpers are not permitted. The source
checkout and its Git metadata must not be group- or world-writable.

### OpenScience workspace release

OpenScience is an independently deployed frontend and is not a service in the
NexPoly production Compose project. Its browser-facing endpoint remains
`http://114.214.255.154:9011/`; both the production NexPoly parent on `9000`
and the development parent on `9001` use that same child endpoint.

`VITE_AGENT_WORKSPACE_URL` is a frontend build-time value. When it is empty,
the immutable Web image renders the "正在同步" placeholder without mounting an
iframe or sending bridge messages. Changing the URL requires building and
publishing a new Web image; setting the variable only in a running container or
production Compose environment cannot change an existing Vite bundle. The
production Web release workflow builds the exact `9011` endpoint into the
image; generic and local builds still default to an empty value.

`http://127.0.0.1:9011/` is valid only when the browser also runs on the server
or when both the NexPoly and OpenScience ports are forwarded over SSH. A remote
browser must use the browser-reachable address above.

The governed OpenScience overlay is published separately as
`ghcr.io/lzq390/openscience-ui:sha-<full-main-sha>`. Its exact deployed base
manifest, static tree, bridge resolver, two call sites, cache-busted asset and
OCI labels are fail-closed CI contracts. The resolver derives the actual parent
from `document.referrer`, accepts only the exact `9000` and `9001` HTTP Origins,
and never uses a wildcard postMessage target.

Use the source-pinned release transaction instead of editing the external
Compose `.env` or recreating the container by hand. `plan` pulls and validates
the candidate digest without changing the live service. Install the lockfile-
exact, script-only Playwright package first; its browsers come exclusively from
the digest-pinned container recorded in the plan. Review the returned plan
digest, then repeat it exactly for `apply`:

```bash
openscience_operation_id=openscience-<utc-timestamp>
openscience_image=ghcr.io/lzq390/openscience-ui@sha256:<reviewed-digest>

npm ci --ignore-scripts --prefix ops/openscience-ui-overlay

./scripts/openscience_ui_release.py plan \
  --sha <full-main-sha> \
  --image "$openscience_image" \
  --operation-id "$openscience_operation_id"

./scripts/openscience_ui_release.py apply \
  --sha <full-main-sha> \
  --image "$openscience_image" \
  --operation-id "$openscience_operation_id" \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest>
```

`apply` starts an isolated candidate on loopback `19011`, checks UI health and
the same-network Backend proxy, then runs a real Chromium bridge probe for both
trusted parents plus rejected Origin/referrer/namespace/version/source cases.
It atomically replaces only the image assignment and recreates only the
OpenScience UI service, then repeats the browser probe against the live
container. Any failed switch automatically restores the exact previous `.env`
payload and image. A completed switch can be explicitly reverted with the same
reviewed plan digest:

```bash
./scripts/openscience_ui_release.py rollback \
  --operation-id "$openscience_operation_id" \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest>
```

If NexPoly moves to HTTPS, an HTTP iframe on `9011` will be blocked as mixed
content and OpenScience must gain an HTTPS endpoint before activation.

### Development tunnel proxy

Online knowledge model extraction can use the optional
`ONLINE_KNOWLEDGE_PROXY_URL` setting. It configures only that OpenAI-compatible
client: the client disables inherited proxy variables, literature discovery
remains direct, and the proxy URL is never returned by the default-config API.
An empty value keeps direct access. The URL must be an absolute HTTP(S) URL
without credentials, path, query, or fragment.

For the current development server, each container uses port `17892` on its
own approved Docker bridge gateway (for example,
`http://172.28.0.1:17892`). Do not assume that
`host.docker.internal:host-gateway` resolves to that gateway on Linux; it may
resolve to the unrelated default bridge instead. A root-managed, bridge-only
TCP relay forwards the gateway endpoints to the active loopback SSH/Codex
proxy. The relay must listen only on the exact Docker gateway addresses and
the firewall must admit only their corresponding bridge subnets; never bind
this unauthenticated development proxy to `0.0.0.0` or the server's public
address.

This is a development-only facility. If the upstream loopback tunnel is absent,
online model extraction may fail while Backend health and all non-model APIs
remain available. Production must use an independently governed outbound proxy
instead of relying on an interactive tunnel.

## Operator workflow

The stable entry point is installed outside the checkout:

```text
/data/lzq/gith/nexpoly-runtime/bin/nexpoly-pull-deploy
```

### Current adopted production authority

The current production runtime has already completed the one-time manual
adoption. Its durable authority is `manual-runtime-adoption`, with
`bootstrap-control.json` schema v3 and `adopted-deployment.json` preserving the
original production provenance. Do not rerun the legacy takeover, bootstrap,
ledger-alias bridge, or the original adoption to perform an ordinary update.
They are retained later in this document only as historical recovery context.

Before the first descriptor-v4 deployment from a newly reviewed `main` SHA,
install the adopted-runtime prerequisites exactly once. Run the source-pinned
script directly from a private, standalone, clean SSH clone whose `HEAD` and
`origin/main` are both the full reviewed SHA and whose protected-main CI has
succeeded. The existing private `mutable-data-audit.pgpass` must already have
been installed by the approved secret provisioner; the installer only
preserves and hashes it and never changes credentials, the database, Git, or a
service.

```bash
prerequisite_operation_id=adopt-prereq-<utc-timestamp>

./scripts/adopt_runtime_prerequisites.py plan \
  --sha <full-main-sha> \
  --operation-id "$prerequisite_operation_id"

./scripts/adopt_runtime_prerequisites.py apply \
  --sha <full-main-sha> \
  --operation-id "$prerequisite_operation_id" \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest>
```

Review the plan's source/tree/CI/adoption authority, every destination digest,
and the preserved pgpass digest before confirmation. `plan` is logically
zero-write. `apply` is create-only and publishes
`state/adopted-prerequisites.json`; it does not restart or reconfigure the
serving runtime. If an uncommitted attempt must be abandoned, use only the same
operation identity and reviewed plan digest:

```bash
./scripts/adopt_runtime_prerequisites.py abort \
  --sha <full-main-sha> \
  --operation-id "$prerequisite_operation_id" \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest>
```

Abort is permitted only before the prerequisite authority commit. It removes
only inode- and digest-matched files created by that operation; a completed
authority cannot be aborted.

A completed prerequisite authority is immutable and must never be rewritten
merely because a verifier is fixed in a later `main` commit. The role
provisioner permits that later target only through a narrow successor binding:
the current private checkout must itself be the clean, standalone protected
remote `main` with successful CI; the sealed prerequisite SHA must be its Git
ancestor; both source trees are recomputed from the full, replacement-free
object database; and all ten fixed prerequisite blobs must have the same
sealed digest at both commits and in their installed destinations. The
preserved pgpass digest must also remain exact. The role plan records the
authority SHA/tree, target SHA/tree, `ancestor-byte-identical` relation, and
sealed file-list digest. A non-ancestor, changed blob, changed installed file,
or pgpass drift remains fail-closed. The prerequisite plan's
`adopted_deployment_sha256` binds the raw bytes of the pre-existing adoption
file; its canonical JSON digest remains a separate bootstrap binding.

### One-time adopted Git permission hardening

The manually adopted checkout did not pass through the historical takeover, so
its production Git modes must be brought under the current controller before
the first formal Pull plan. This is a separate, one-time transaction after
`adopted-prerequisites.json` is complete. It publishes
`state/adopted-git-permissions.json` with
`authority_kind=manual-runtime-adoption-permission-hardening`; it does not
rewrite the original adoption, prerequisite, or bootstrap authority.

Run all three verbs from the same private, standalone, clean, complete-history,
source-pinned target clone used for the prerequisite transaction. On the
initial plan and the first apply that writes durable `intent`, `HEAD` and
`origin/main` must both be the full reviewed SHA and protected-main CI must
still be successful. After durable intent exists, same-operation replay or
abort uses the sealed source and confirmed digests; it deliberately does not
re-query a moving `origin/main` or CI result, so a later main advance cannot
block the only safe recovery path:

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
`20260814t140705z`, matching the operation-ID grammar.

`permission-plan` is logically zero-write. Review its complete Git permission
inventory, each current and target mode, production source identity, target
source SHA/tree, and raw adoption/bootstrap/prerequisite bindings. The target
policy makes the checkout root and all Git metadata directories mode `0700`;
Git metadata files retain only their existing owner permission bits. In other
words, the complete mutation scope is the checkout root and `.git/**`; ordinary
working-tree files are excluded. The `plan_sha256`
binds that complete plan; the separately confirmed
`permission_impact_sha256` binds the exact inode mode transitions. The current
production observation contains 167 such transitions, but 167 is not a policy
constant or an operator input. The count and records in the fresh reviewed plan
are authoritative. Any different count, path, mode, inode identity, or either
changed digest requires a new review and must stop apply.

`permission-apply` acquires the shared `state/deploy.lock`, repeats the source
and authority checks, and compares the reviewed plan and impact under the lock
before the first mode change. It changes only the planned checkout-root/Git
metadata modes and its private marker/journal/authority; it does not change Git
content or refs, PostgreSQL, containers, credentials, or service state. Its
private journal at
`state/adopted-git-permission-transactions/<operation-id>.json` advances
monotonically: `intent` → `permission-change-intent` → `permission-ready`
→ `source-verified` → `authority-commit-intent` → `completed`. Every
checkpoint and directory update is durably synced. The journal and the
primitive permission marker both compare the planned device/inode/type/mode
and content evidence, so an unplanned path, replacement, source drift, raw
authority drift, or concurrent deployment fails closed instead of widening the
reviewed mutation.

The primitive marker keeps its immediate predecessor plus content-addressed
retired generations for the complete finite lifecycle. Those files are
authority, not temporary cleanup: never delete or rename
`.legacy-git-permission-takeover.json.previous` or any matching
`.legacy-git-permission-takeover.json.retired-g*` entry. Rotation uses
no-replace renames and validates the contiguous generation chain on every
replay. Before the first marker write or `chmod`, the helper proves that the
complete retained history is at most 512 MiB and that the state filesystem has
room for every remaining generation plus a 64 MiB safety margin.

Abort is available only while the transaction is still before the durably
written `permission-change-intent`, using the same operation and both reviewed
digests:

```bash
./scripts/adopt_runtime_prerequisites.py permission-abort \
  --sha <full-main-sha> \
  --operation-id "$permission_operation_id" \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \
  --confirm-permission-impact-sha256 sha256:<reviewed-impact-digest>
```

Before that boundary, abort records only that exact transaction as aborted.
At or after `permission-change-intent`, a crash may have committed any prefix
of the planned `chmod` operations, so abort and permission restoration are
forbidden. Recovery is forward-only: rerun `permission-apply` with the same
operation ID, SHA, and two confirmations until `completed`. A new operation,
an inferred rollback, or hand-edited journal/authority is not recovery.
`authority-commit-intent` is likewise an unknown wrapper-publication boundary
and converges only through the same apply.

The completed wrapper seals both target and production source identities; the
raw `adopted-deployment.json`, `bootstrap-control.json`, and
`adopted-prerequisites.json` digests; both confirmation digests; the full plan;
and the raw permission marker, evidence, inventory, original-permission, and
hardened-permission digests. It is immutable. The schema-v1 hardened marker at
`state/legacy-git-permission-takeover.json` is retained only as a format-
compatible handoff that the already installed controller can verify. The
permission transaction does not invoke that old controller as a probe. The new
target controller independently requires the wrapper and marker to bind each
other, seals the compact permission projection together with the
unit-permission authority in its historical schema-v3 adopted-prerequisite
target binding, and separately revalidates
both authorities at plan, prepare, resume, and apply pre-switch validation.
Schema-v2/schema-v3 remain read compatibility for historical descriptor
provenance only. Once source-successor lineage exists, a new raw-adoption
descriptor uses schema-v4 and cannot omit the old root, source-successor, or
unit authority.

Do not call `git_source_trust.takeover_repository_permissions` directly, run
`install_legacy_takeover_prerequisites.py`, start the historical takeover, use
manual `chmod`, or synthesize the legacy marker. Those raw paths do not publish
the adopted permission authority or its double-confirmed CAS and cannot
authorize the current production checkout.

### One-time Git permission source-successor authority

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

First create the independent whole-directory golden snapshot. `plan` is
logically zero-write. `apply` copies every `.git` byte without hard links or
reflinks, runs strict `git fsck`, and publishes the create-once
`state/production-git-snapshot.json` authority:

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

The snapshot does not alter production Git, services, containers, units, or
PostgreSQL. Its independent backup and manifest remain immutable for the
entire first-deployment authority chain.

If a reviewed first-deployment target changes either control blob governed by
the immutable adopted Git-permission authority, the byte-identical successor
rule must not be weakened or bypassed. Publish the single fixed successor
authority before the Worker-unit transaction:

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

The standalone publisher is standard-library-only. It never imports or
executes the candidate `bootstrap_pull_deploy.py`, `git_source_trust.py`, or
`bridge_deploy_core.py`. Instead it loads the bootstrap and Git-trust
verifiers from the source SHA already sealed by
`state/adopted-git-permissions.json`, freezes that authority's required CI job
set, and requires the target bridge CI-contract blob to remain byte-identical.
Its independent checkout verifier and the frozen verifier must agree on the
exact clean, private, standalone main checkout, source SHA/tree, and protected
CI run/attempt.

The predecessor transaction journal's `source_trust_sha256` is historical
evidence, while `production_source_trust_sha256` is a fresh proof of the still
unchanged live production source. In the successor plan these are named
`predecessor_source_trust_sha256` and `production_source_trust_sha256`;
they are separate evidence and must not be compared for equality. Adoption may
have added a legitimate remote tracking ref without changing production HEAD,
tree, or worktree content.

The plan additionally seals a three-state repository transition. `B` is the
exact adopted baseline with direct-commit local `main`, the direct deploy
remote at the predecessor authority, and no prepared ref. `F` has the deploy
remote and exact semantic object union materialized at the target but no
prepared ref. `P(operation)` is `F` plus the one current operation-owned
prepared ref. Formal plan accepts only `B` or `F`; target prepare/resume and
apply pre-switch accept only the exact current `P(operation)`. Empty
`refs/nexpoly` directories left after an abort are permitted, but another
operation's ref is not.

The sealed transition covers the stable checkout/config/index projection,
baseline logical/raw refs and auxiliary metadata, all baseline semantic
objects, the exact target closure, and the exact expected union. After fetch,
strict `fsck` and the union proof reject both added valid dangling objects and
lost baseline objects. Only canonical loose objects, pack/index/rev sidecars,
and supported commit-graph storage are valid. Any `.lock`, `tmp_pack_*`,
incomplete sidecar, unexpected ref or object path, symref substitution, or
packed-ref drift stops the release. `FETCH_HEAD` must be the exact single
protected-main SSH record. A pre-existing deploy reflog must preserve its
sealed prefix and may add only the exact fast-forward record. Descriptor v4
seals the post-fetch full-trust, semantic, physical object-storage,
logical/raw-ref, and auxiliary digests; resume, already-ready, and pre-switch
validation compare all of them exactly.

After an ordinary network interruption, retry only if the repository proves as
clean `B`, `F`, or the original `P(operation)` with no lock or temporary pack.
A power loss during the old controller's fetch can instead leave a broken ref
or incomplete object transaction that fails before a retry can refetch. This
is an explicit stop condition, not permission to remove a lock or repair one
ref manually. Preserve traffic and services, stop the release, and restore the
entire `.git` directory from a separately verified owner-private pre-prepare
snapshot (or follow a separately reviewed recovery procedure). Re-run
plan/prepare with a new operation ID unless the original `P(operation)` is
independently proved exact.

After making the terminal choice to abandon the interrupted operation, use
the dedicated restore transaction; never repair individual refs. Review the
zero-write plan, then repeat its snapshot-authority confirmation exactly:

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

The restore atomically exchanges the entire `.git` directory, materializes a
clean worktree from the restored authority, and archives the displaced Git
directory. The golden copy is never used as the live directory and remains
unchanged for independent verification.

The fixed 13-file manifest contains exactly the ten prerequisite install source
paths plus `scripts/bootstrap_pull_deploy.py`, `scripts/git_source_trust.py`,
and `scripts/bridge_deploy_core.py`; it is never populated by discovery. It
seals Git object type, mode, blob ID, and content digest for both predecessor
and target. The only authorized changed paths are exactly
`scripts/bootstrap_pull_deploy.py` and `scripts/git_source_trust.py`; every
other record must be unchanged. Symlinks, submodules, mode drift, another
changed path, a non-ancestor target, or any old
authority/marker/adoption drift fails closed.

`plan` is logically zero-write. `apply` holds `state/deploy.lock`, repeats the
protected-main/CI admission before its first durable intent, and may create
only its journal plus the fixed, mode-`0600`, create-once authority at
`state/adopted-git-permission-source-successor.json`. It does not change Git,
permissions, units, services, containers, PostgreSQL, credentials, or source
files. The journal at
`state/adopted-git-permission-source-successor-transactions/<operation-id>.json`
advances `intent` → `predecessor-verified` → `source-verified` →
`authority-commit-intent` → `completed`. Once commit intent is durable,
recovery is forward-only with the same operation, SHA, plan digest, and
source-successor impact digest.

Before commit intent, the same confirmed operation may be aborted:

```bash
./scripts/adopt_git_permission_source_successor.py abort \
  --sha <full-main-sha> \
  --operation-id "$source_successor_operation_id" \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \
  --confirm-source-successor-impact-sha256 sha256:<reviewed-impact-digest>
```

The old Git authority and marker remain immutable and are never replaced.
Deleting, altering, or partially publishing the new fixed authority is a
permanent lineage sentinel and must never fall back to the old byte-identical
interpretation. The controller retains the complete old-root →
source-successor → unit-authority chain.

### One-time adopted Worker unit permission hardening

The Worker-unit command remains prohibited until the snapshot and source-successor authorities are complete.

Before the first formal Pull plan, run one final independent transaction to
replace only the legacy mode-`0664` MD user-unit inode with an owner-private
mode-`0600` inode. DFT is already mode `0600` and is a strict no-op CAS. Run
the plan from the exact source SHA sealed by the source-successor authority.
The unit plan binds both the immutable root Git authority and the new raw
source-successor authority; it must not use the historical byte-identical
fallback when any successor lineage path exists:

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

The plan is logically zero-write and seals each unit and its parent by path,
type, device/inode, owner, mode, link count, size, and content digest, plus the
live systemd state, MainPID, and InvocationID. During apply the shared parent
directory's identity, ownership, mode, and link count remain exact; only its
reported size may grow as a filesystem allocation side effect of the
transaction's own temporary entry. It also binds the raw adoption,
bootstrap, prerequisite, and Git-permission authorities. Apply holds
`state/deploy.lock`, writes an operation-owned private backup, creates a new
`0600` inode, and uses atomic name exchange; it never calls `chmod` on the live
MD inode. It runs `systemctl --user daemon-reload` but neither stops nor
restarts either Worker, and requires both process identities to remain exact
and `NeedDaemonReload=no` before publication.

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

The journal at
`state/adopted-unit-permission-transactions/<operation-id>.json` is replayed
forward across every replacement/publication crash window. Abort is permitted
only before durable `replacement-intent`:

```bash
./scripts/adopt_runtime_prerequisites.py unit-permission-abort \
  --sha <full-main-sha> \
  --operation-id "$unit_permission_operation_id" \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \
  --confirm-unit-permission-impact-sha256 sha256:<reviewed-impact-digest>
```

After replacement intent, rerun only the same apply with the same SHA,
operation ID, and confirmations. The immutable result is
`state/adopted-unit-permissions.json`; it embeds the complete original and
hardened unit records and private-backup evidence with independently
recomputable digests. A narrowly validated prepare-abort residue at
`operation-archive-intent` is allowed only when no descriptor or `ready.json`
exists, its handoff/ref evidence is archived, and the live prepared ref and
handoff are absent. Any other prepared state blocks this transaction.

For this source-successor-bearing raw adoption, the unit plan, journal, and
final `state/adopted-unit-permissions.json` authority must all use schema v2;
schema v1 remains historical read compatibility only. The resulting raw
adoption must emit descriptor v4 and, after a successful deployment,
current-state v3. That outer descriptor v4 contains a schema-v5 adopted-
prerequisite target binding. It binds the immutable old root, raw source-
successor authority, its completed journal, the schema-v2 Worker-unit
authority, that unit authority's completed journal, the complete unit
transaction inventory, and the raw snapshot/router anchors. Historical inner
binding schemas v2-v4 remain read compatibility only. The inventory must
contain exactly one completed journal; every other entry must be a valid
terminal no-mutation abort. A projected authority, schema-v1 unit authority,
nonterminal journal, or unsealed inventory entry is not a substitute.

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
anchor form; an authorized successor binding preserves every historical value
while upgrading the record. After that upgrade, once written, all eight
anchors are permanent: every later current-state v3 must preserve their exact
values, and deletion or replacement fails closed.

### One-time bootstrap router successor

After the unit authority completes, publish the selector successor. It
installs the content-addressed target control release and immutable snapshot
verifier, but keeps `active-control` and both Worker routes unchanged. A
temporary `state/contract-0012-in-progress.json` fence blocks the old
controller before its first Git write throughout every selector-swap crash
window:

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

The create-once `state/bootstrap-router-successor-intent.json` records the
single-file selector CAS and its complete predecessor chain. The completed
`state/bootstrap-router-successor.json` uses
`manual-runtime-adoption-bootstrap-router-successor`. The new selector
explicitly rejects the first-deployment `plan`, which must remain the direct
read-only private-clone exception documented below. Before `prepare` performs
the first live Git write, the selector runs snapshot `verify-integrity` and
routes only the sealed target to its preinstalled control release. Missing,
partial, or drifted authority blocks deployment. Once current-state v3 is
durably target-bound and the exact T control release is active, the one-time
target route retires; a merely present or forged current-state fails closed.
The selector and all eight lineage anchors remain permanently validated.

Next provision the dedicated mutable-data audit login. This step is mandatory
before formal Pull `plan` or `prepare`. The source-pinned provisioner reads the
one exact private pgpass entry, derives a SCRAM verifier locally, and never
places the plaintext password in SQL, argv, JSON, journals, or logs:

```bash
role_operation_id=mutable-role-<utc-timestamp>

./scripts/provision_mutable_data_audit_role.py \
  --sha <full-main-sha> \
  --operation-id "$role_operation_id" \
  --plan

./scripts/provision_mutable_data_audit_role.py \
  --sha <full-main-sha> \
  --operation-id "$role_operation_id" \
  --apply \
  --confirm-plan-sha256 sha256:<reviewed-plan-digest> \
  --confirm-public-lo-acl-sha256 sha256:<reviewed-public-lo-impact-digest>
```

The schema-v7 mutable-data evidence is an explicit least-privilege projection:
the role has no elevated attributes, role memberships, object ownership, or
cluster-wide predefined read role. Its normalized direct grants are `CONNECT`
on `nexpoly`, `USAGE` on the governed schemas that exist, `SELECT` on their
current tables/views and sequences, and schema-scoped future `SELECT` defaults
for objects created by `polyprop`. It has no create/write authority, column
write grant, direct function grant, authority outside the governed schemas, or
execution of security-definer routines. The independently confirmed impact
also removes PUBLIC execution from the eight large-object mutators while
retaining the database owner's authority. Review both confirmation digests
before apply.

### Historical bootstrap and F→B bridge only

The remainder of this operator-workflow section records the former
takeover/bootstrap/bridge path for provenance and recovery analysis. It is not
the update procedure for the already adopted production runtime. Do not execute
it merely because a new ordinary release is available.

Before bootstrap, provision the following deploy-user-owned files outside the
checkout. Directories are mode `0700`; credential/config files are mode `0600`;
bootstrap executables are mode `0700`:

- `config/git-deploy-key`: a repository-scoped, read-only GitHub deploy key;
- `config/known_hosts`: a reviewed, pre-pinned GitHub host key (runtime
  `ssh-keyscan` is forbidden);
- `config/github-api-token`: a token limited to reading Actions run/check
  evidence;
- `config/docker/config.json`: inline read-only GHCR authentication for
  `ghcr.io`, with no external credential helper;
- `config/deploy.env` and `config/app.env`: production control and application
  values; passwords, tokens and DSNs are never command-line arguments;
- four reviewed wrappers: `config/bootstrap-quiesce`,
  `config/bootstrap-status`, `config/bootstrap-resume-unchanged`, and
  `config/bootstrap-rollback`;
- four site-specific helpers: `config/bootstrap-active-jobs-probe`,
  `config/bootstrap-legacy-runtime-status`,
  `config/bootstrap-legacy-runtime-resume-unchanged`, and
  `config/bootstrap-legacy-runtime-restore`.
- the source-read-only
  `config/contract-0012-external-database-audit` helper, its source-pinned
  `nexpoly-postgres-media-evidence` builder, tracked static authority rules,
  generated private schema-v5 runtime registry and exact PG14/15/16/18
  read-only audit image map.
  The audit image map is distinct from the actual-operation PG16 restore image
  used for logical backup recovery and the 0005 alias gate. The helper
  enumerates arbitrary-named PostgreSQL volumes, PGDATA
  bind mounts, all three post-takeover private backup roots and the independent
  dev/health
  stacks. Dormant media are copied before a matching-major PostgreSQL audit
  process starts; logical backups restore only with the separately fixed PG16
  image in network-none disposable clusters. Any medium containing the
  superseded 0013 checksum freezes B and requires a new 0014 correction rather
  than rewriting 0013.
  A successful isolated audit also publishes an owner-private mode-`0600`
  checkpoint below `audit/postgres-media/.audit-checkpoints`. Each checkpoint
  is bound to the exact source descriptor, static authority-rules digest,
  complete auditor digest, compiled discovery boundary and every local
  PG14/15/16/18 image ID. The first capture uses `build`; subsequent Pull
  gates use the fixed `revalidate` mode. Revalidation still repeats the whole
  Docker/backup discovery, live role/database audit and final offline content
  CAS, but it does not copy, start or restore an unchanged dormant cluster.
  A missing, altered or source-mismatched checkpoint fails closed.

  Before the future takeover, the approved private-archive provisioner must
  create the independent recovery root without adding any dump:

  ```bash
  install -d -m 0700 /data/lzq/recovery/nexpoly-postgres-media
  [[ "$(stat -c '%u:%a' /data/lzq/recovery/nexpoly-postgres-media)" == \
     "$(id -u):700" ]]
  ```

  After takeover, readiness must additionally prove that the takeover-created
  `runtime/legacy-takeover/preserved-postgres-backups` root and the fixed
  dirty-0009 quarantine root both exist as deploy-user-owned mode-`0700`
  directories. Missing roots are blocking; the current main repair does not
  create or modify any of them.
- the reviewed, non-secret
  `ops/config/postgres-media-audit-role.sql.example` contract for the online
  NOLOGIN audit roles. A matching-major pinned client joins the exact active
  container's network namespace and connects to `127.0.0.1`; it does not run
  `psql` in the target container, use DNS, or use a host-published port. The
  helper connects as the inspected `POSTGRES_USER` and enters those
  membership-free roles with `SET LOCAL ROLE`. Its fixed launcher opens only
  `runtime/config/postgres-media-credentials.json`, validates it as one
  deploy-user-owned mode-`0600` regular file, and passes the already-open file
  plus its digest to the exact-F auditor. The schema-v1 envelope binds each
  current password to the exact original container ID, cluster system
  identifier, inspected administrator and PostgreSQL major. Stale
  `POSTGRES_PASSWORD` values in container launch metadata are ignored and
  `POSTGRES_PASSWORD_FILE` is rejected. The password crosses neither host argv
  nor host env: it is framed through stdin into a private 0600 pgpass file on
  the pinned disposable client's tmpfs. `pg_control_system()` must match the
  sealed system identifier before requested SQL runs.

  Generate the real envelope from
  `ops/config/postgres-media-credentials.json.example` only through the
  approved secret provisioner. The placeholder template is intentionally
  invalid, contains no secret, and must never be copied unchanged. Install the
  real file before the future maintenance window; do not commit, bundle,
  print, source or pass it as a command-line argument. Direct trust mode is
  limited to ephemeral integration tests and cannot pass through the installed
  launcher. Do not invoke the SQL file manually. The stable helper derives the
  authority-rules, role-SQL, launcher and implementation digests from the
  validated active F control manifest; absent digest environment variables are
  supported for this operator path, while any caller-supplied mismatch is
  rejected. This entry cannot run until takeover has published the preserved
  backup root and Bootstrap has activated F; use only the chronological
  post-bootstrap step below.

  `role-plan` is read-only with respect to source databases and binds every
  database OID/owner, inspected session administrator, container attachment,
  Docker/source epoch, exact F role-SQL digest, runtime registry and the
  pre-provision cluster-global role matrix. That matrix checks every managed
  marker role in every connectable database; orphan markers, reused role names,
  cross-database ACL/default-ACL/ownership and any non-target write capability
  all block before SQL. Review that private JSON before confirmation.
  `provision-roles` is the only
  mutating phase: the launcher passes manifest-pinned SQL by inherited file
  descriptor, re-CASes the container/database immediately before and after
  every idempotent transaction, and the transaction verifies session user,
  database OID and owner. After a lost response or partial multi-database
  failure, generate and review a fresh plan; never reuse a stale confirmation.
  The first bridge `prepare` performs the one mandatory fresh full `build`;
  operators do not run a second manual build. All later 0012 and steady-state
  captures use `revalidate` and must not regenerate the registry.
- a reviewed, non-secret
  `ops/config/mutable-data-audit-role.sql.example` provisioning/check
  contract. It is executed only by the source-pinned provisioner described
  above, never manually. Provision the `nexpoly_mutable_audit` password out of
  band and install only its mode-`0600` pgpass value. The helper's schema-v7
  evidence seals the complete explicit grants described above and rejects any
  elevated attribute, membership, ownership, write authority, direct function
  grant, security-definer execution, or authority outside the governed schemas.
  One read-only,
  deferrable, repeatable-read transaction also seals the canonical PolyTAO
  row/schema/structure archive while 0012 is pending. The controller requires
  that embedded seal to equal the later full backup/isolated-restore evidence
  byte for byte before it may publish the transaction guard; after 0012 the
  field must be exactly `null`.
- `bootstrap-input/legacy-takeover-classification.json`, mode `0600`, covering
  the production checkout's ignored paths exactly with `runtime`, `secret` or
  `asset` classifications. The reviewed file must contain no secret value.

All executables are fixed-name, deploy-user-owned mode `0700` files. The
deployment descriptor seals every bootstrap hash and the 0012 adapter
independently seals its audit-helper hash. The status and database-audit helpers
are read-only; the unchanged-resume helper may restore ingress only and must
prove the Backend and Worker processes did not restart; the full restore helper
is reserved for a runtime already stopped or partially replaced. Each helper
must be idempotent across a lost response. There are no configurable
shell-command selector variables.

The site-specific helpers and classification are first staged under the
deploy-user-owned mode-`0700` `bootstrap-input` directory. The shipped
site-specific `.example` files contain `SITE_IMPLEMENTATION_REQUIRED` and are
deliberately rejected unchanged. The source-pinned prerequisite installer
copies the reviewed wrappers, customized helpers, classification, validator
and takeover recovery launcher under one non-blocking
`state/deploy.lock`. It compares every repository payload with the exact F Git
blob and rechecks the standalone F source before reading, before installation
and after installation:

```bash
./scripts/install_legacy_takeover_prerequisites.py \
  --authority-sha <full-F-sha> \
  --authority-tree <40-character-F-tree>

./scripts/install_legacy_takeover_prerequisites.py \
  --authority-sha <full-F-sha> \
  --authority-tree <40-character-F-tree> \
  --apply
```

The resulting private
`legacy-takeover/INSTALL-MANIFEST.json` binds F SHA/tree, every installed hash,
the helper-readiness digest and the classification digest. A conflicting
existing target is never overwritten.

The installed content-addressed control release exposes a non-executing
readiness check. It verifies owner/mode/path identity and hashes every helper,
but deliberately does not invoke recovery helpers:

```bash
/data/lzq/gith/nexpoly-runtime/bin/control_runtime_selector.py \
  run site-helper-readiness readiness
```

Captured site-helper JSON can be validated separately with
`site-helper-readiness validate --helper <fixed-name> --input <private-json>`.

Control compatibility also declares `prepare_abort_abi_versions: [1]`.
ABI v1 fixes the ordinary/bridge descriptor schemas and the complete set of
prepare-owned resources: the operation tree and staging tree, monomer-MD slot
tree/record/staging quarantines, content-addressed wheel-cache staging,
selector handoff, and `refs/nexpoly/prepared/<operation>`. The active release
refuses to hand preparation to a candidate that does not declare this ABI.
`prepare-abort` atomically moves each exact, intent-sealed resource into one
owner-private archive, archives the handoff and Git-ref provenance, and
CAS-deletes only the exact prepared ref. Final F must retain ABI v1 and may not
add a production prepare artifact or descriptor field. Such a change requires
a new bridge release that can generically recover both ABI generations before
it can become an upgrade target.

The initial controller must be installed from a clean temporary standalone
clone at the reviewed base SHA/tree, beneath an owner-controlled directory that
is not group/world writable. A shared Git worktree is rejected because its
common local config is outside the clone boundary. The clone must contain no
ignored paths, executable Git filters or writable Git policy files. Do not
bootstrap from a dirty development worktree or with a personal credential
helper. Git alternates, `commondir`, `--shared`, `--reference` and locally
hard-linked object databases are rejected; clone from the canonical remote (or
use `--no-local`) into a source path disjoint from both production and runtime.
For example, create the source under a private parent with `umask 077`:

```bash
umask 077
git clone --no-local git@github.com:lzq390/ZhijuPoly.git \
  /home/devuser/nexpoly-bootstrap/source
```

Invoke the script
directly through its fixed isolated-Python shebang, or explicitly with
`/usr/bin/python3 -I -B`; ordinary `python3 scripts/...` is rejected. First run
the standalone source-readiness check. It performs no fetch or maintenance and
rejects shared worktrees/object stores, shallow history, ignored content,
unreachable objects, writable paths, a non-canonical origin, and any SHA
mismatch:

```bash
./scripts/bootstrap_pull_deploy.py \
  --check-source-readiness \
  --source-root /home/devuser/nexpoly-bootstrap/source \
  --sha <main-sha>
```

Before stopping service, prefetch and verify the exact F authority, policy-
pinned B commit/tree and OCI digests, schema-v2 asset, wheels and restore tools.
The prefetch evidence must be complete while ingress is still open; no mutable
tag or 90-day artifact is an authority. Use one operation ID throughout the
later bridge plan and prepare. The authority image references are the exact F
GHCR digest references; the base-Python path and identity must be copied
verbatim from the sealed runtime `deploy.env`:

```bash
prefetch_operation_id=prefetch-<utc-timestamp>
/data/lzq/gith/nexpoly-runtime/legacy-takeover/bin/nexpoly-maintenance-prefetch \
  --source-root /home/devuser/nexpoly-bootstrap/source \
  --runtime-root /data/lzq/gith/nexpoly-runtime \
  --operation-id "$prefetch_operation_id" \
  --authority-backend-image ghcr.io/lzq390/nexpoly-backend@sha256:<64-lowercase-hex> \
  --authority-web-image ghcr.io/lzq390/nexpoly-web@sha256:<64-lowercase-hex> \
  --docker-config /data/lzq/gith/nexpoly-runtime/config/docker \
  --base-python /home/devuser/miniconda3/envs/byteff2-repro/bin/python \
  --base-python-identity-sha256 sha256:<64-lowercase-hex>
```

Review the emitted `status=ready`, exact F/B source identities, images, wheel
caches, asset and recovery-tool inventories before continuing. Then seal and
apply the legacy takeover using only the installed recovery launcher and the
classification digest from the install manifest:

```bash
/data/lzq/gith/nexpoly-runtime/legacy-takeover/bin/nexpoly-legacy-takeover \
  seal \
  --operation-id takeover-<utc-timestamp> \
  --classification-sha256 sha256:<64-lowercase-hex>

/data/lzq/gith/nexpoly-runtime/legacy-takeover/bin/nexpoly-legacy-takeover \
  apply \
  --operation-id takeover-<utc-timestamp>
```

Takeover drains to canonical zero active jobs, stops only the sealed Web,
Backend and user Worker, leaves the exact PostgreSQL container/image/volume
and system identifier running, backs up the Worker unit and any prior
`bin`, `control-releases`, `active-control.json`, `bootstrap-control.json`,
Bootstrap Worker-unit intent/completion audit and Bootstrap Worker-unit backup
directory, externalizes the exact classified inventory, and CAS-switches the
canonical HTTPS origin to SSH. It also seals mode/owner/type for every existing
production checkout and `.git` path outside the classified subtrees; later Git
objects may be added, but no sealed path may disappear or change type. Its
public status schema binds the operation ID, classification, legacy runtime,
Git identity, pre-stopped fence, original control-layout and checkout-
permission digests, and final applied-record digest.

Only after takeover reports `apply_phase=complete` may the F bootstrap control
plane be planned:

```bash
./scripts/bootstrap_pull_deploy.py \
  --sha <main-sha> \
  --legacy-takeover-operation-id takeover-<same-utc-timestamp> \
  --production-root /data/lzq/gith/nexpoly \
  --runtime-root /data/lzq/gith/nexpoly-runtime
```

After checking the reported source tree, CI attempt, filesystem inventory and
current Worker unit SHA-256, initialize the control plane. The dry-run does not
execute Git from the still-writable production checkout; exact branch, remote,
HEAD, tree, clean/ignored state and fast-forward validation occurs during the
confirmed apply only after Bootstrap has locked directory writes, rejected
executable Git policy and atomically replaced Git config/attributes with
private inodes:

```bash
./scripts/bootstrap_pull_deploy.py \
  --sha <main-sha> \
  --legacy-takeover-operation-id takeover-<same-utc-timestamp> \
  --apply \
  --production-root /data/lzq/gith/nexpoly \
  --runtime-root /data/lzq/gith/nexpoly-runtime \
  --confirm-production-root /data/lzq/gith/nexpoly \
  --confirm-runtime-root /data/lzq/gith/nexpoly-runtime \
  --confirm-source-tree <40-character-tree> \
  --confirm-worker-unit-sha256 sha256:<64-lowercase-hex>
```

Bootstrap consumes the named, already-installed takeover operation and fence
from private runtime state; callers supply its operation ID but none of its
digests. It atomically
installs F controls, replaces the exact mode-`0664` Worker unit under CAS, runs
`daemon-reload`, and records the takeover authority. Never pre-`chmod`, replace
or manually reload that unit. Remove the temporary bootstrap source only after
the installed immutable inventory and completed bootstrap authority verify.

### Post-bootstrap external-media role provisioning

Only now—after takeover has published the preserved backup root and Bootstrap
has activated exact F controls—may the operator prepare audit roles. First
verify all three fixed backup roots are present with the owner/modes documented
above, then run the installed helper:

```bash
/data/lzq/gith/nexpoly-runtime/bin/nexpoly-postgres-media-evidence role-plan
/data/lzq/gith/nexpoly-runtime/bin/nexpoly-postgres-media-evidence \
  provision-roles --confirm-plan-sha256 sha256:<reviewed-plan-digest>
```

Review the complete plan before confirming it. If provisioning is interrupted,
generate and review a new plan. Do not invoke the zero-argument build manually:
the following exact `bridge-prepare` is the sole fresh registry build, while
all later gates use `revalidate`.

### One-time production ledger-alias gate

The audited legacy production ledger contains one duplicate historical alias,
`0005_polytao_jobs`. The first full Pull deployment is deliberately blocked
until the fixed-purpose reconciliation control has removed exactly that ledger
row. Bootstrap installs this control outside the still-legacy checkout. Complete
Pull `bridge-plan` and `bridge-prepare` for the exact policy-selected B before
reconciling the alias, then run the already prepared `bridge-apply`. Ordinary
`plan`/`prepare`/`apply` against current main are not a substitute for this
one-time bridge. Once an alias operation marker exists but is incomplete, all
Pull commands remain blocked until that same alias operation recovers. Do not
update the production checkout first.

Recover the exact prefetch operation ID recorded by the earlier ready evidence
and use one new bridge operation ID for all three bridge commands. The caller
supplies only F; `ops/config/production-bridge-policy.json` selects B:

```bash
prefetch_operation_id=prefetch-<recorded-utc-timestamp>
bridge_operation_id=bridge-<utc-timestamp>

nexpoly-pull-deploy bridge-plan \
  --authority-sha <full-F-sha> \
  --operation-id "$bridge_operation_id" \
  --prefetch-operation-id "$prefetch_operation_id"

nexpoly-pull-deploy bridge-prepare \
  --authority-sha <full-F-sha> \
  --operation-id "$bridge_operation_id" \
  --prefetch-operation-id "$prefetch_operation_id"
```

Review the plan and READY descriptor and finish `bridge-prepare` before the
alias maintenance begins.

Provision the one-line production DSN out of band into a deploy-user-owned
mode-`0600` credential file. It must use the pinned
`polyprop@127.0.0.1:55432/nexpoly?sslmode=disable` endpoint and must never
appear in a command argument, terminal input, shell history, log or audit
record. Load it without echoing the value; the first invocation is read-only:

```bash
install -d -m 0700 /data/lzq/gith/nexpoly-runtime/config
credential=/data/lzq/gith/nexpoly-runtime/config/production-postgres.dsn
if [[ ! -e "$credential" ]]; then
  install -m 0600 /dev/null "$credential"
  echo "Populate $credential through the approved secret provisioner, then rerun." >&2
  exit 1
fi
[[ -f "$credential" && ! -L "$credential" ]]
[[ "$(stat -c '%u:%a' "$credential")" == "$(id -u):600" ]]
IFS= read -r NEXPOLY_PRODUCTION_POSTGRES_DSN \
  < "$credential"
[[ -n "$NEXPOLY_PRODUCTION_POSTGRES_DSN" ]]
export NEXPOLY_PRODUCTION_POSTGRES_DSN
/data/lzq/gith/nexpoly-runtime/bin/nexpoly-reconcile-production-0005-polytao-alias \
  --operation-id alias-0005-<utc-timestamp>
```

After reviewing the exact cluster, ledger, dynamically sealed business-row
archive, PostgreSQL client, backup and isolated-restore plan, repeat the same
operation ID with the explicit write confirmation during a maintenance window. Isolate ingress; stop the
Backend, Web/Nginx, `postgres-init`, and all MD/DFT Worker processes while
keeping PostgreSQL running. Apply refuses a production database with any other
client session:

```bash
/data/lzq/gith/nexpoly-runtime/bin/nexpoly-reconcile-production-0005-polytao-alias \
  --operation-id alias-0005-<utc-timestamp> \
  --apply \
  --confirm-database nexpoly
```

The tool accepts no database, migration, checksum, SQL, audit-root or backup
path selector. It pins the production cluster identity, canonical 0001-0008
ledger, alias tuple, PolyTAO archive/schema digests and PostgreSQL 16 execution
surface. Apply holds the shared deployment lock and database locks across a
full custom dump, digest/list evidence, an isolated PostgreSQL 16 restore,
lock-internal revalidation and a one-row compare-and-swap delete. Any unknown
commit result remains fenced by its durable operation marker and is recoverable
only with that same operation ID.

With the alias marker complete, apply the already prepared exact B bridge:

```bash
nexpoly-pull-deploy bridge-apply \
  --authority-sha <full-F-sha> \
  --operation-id "$bridge_operation_id"
```

The first governed takeover is the sole exception to “deploy current main”.
After crash-safe legacy takeover has produced a clean SSH checkout and
bootstrap has installed the current F control plane, the operator supplies
only F—not a historical target—to these bridge commands.

F's `ops/config/production-bridge-policy.json` is the only source of B. It pins
the full B commit/tree, the private `refs/nexpoly/bridge-target/<B>` ref, image
digests, the schema-v2 asset digest, the empty dataset-rebuild list, accepted
migration ledgers and all required F CI jobs. F derives and installs B controls;
B then independently fetches current main, rereads the same F policy, proves
B→F ancestry and the production-HEAD→B fast-forward, and CAS-creates the exact
private ref. Tags, branch names, short SHAs and caller-supplied historical SHAs
are never accepted. The v3 descriptor keeps F authority and B target evidence
separate. Source switch merges the exact private B ref, never remote main.

A private global bridge token is reserved before READY, bound to the exact
descriptor digest, moved to commit-intent only after the candidate state is
sealed in the crash marker, and permanently consumed only with that exact
current-state digest. Recovery forwards a durable commit-intent if a crash
occurred immediately before current-state rename; a consumed token without its
current state fails closed. Ordinary v2 deployments neither consume nor reset
this one-time authority.

Before invoking the legacy restore, B publishes a minimum owner-private
recovery capsule outside the control layout that restore removes. The capsule
binds the exact B recovery entry, descriptor, authority SHA, target SHA and
control release by SHA-256. If the process crashes after legacy restore has
reinstated the old HTTPS checkout and permissions, only the source-pinned
recovery launcher may finish bookkeeping:

```bash
/data/lzq/gith/nexpoly-runtime/legacy-takeover/bin/nexpoly-bridge-recover \
  --capsule-sha256 <marker-capsule-sha256> \
  --authority-sha <full-F-sha> \
  --target-sha <full-B-sha> \
  --operation-id <bridge-operation-id> \
  --descriptor-sha256 <marker-descriptor-sha256> \
  --restored-terminal-sha256 <takeover-terminal-sha256>
```

That entry cannot touch Git, containers, services, the database, assets or
admission. Under the shared deploy lock it only revalidates the exact terminal
legacy restore, finalizes the marker, writes the terminal audit and failed
operation state, changes a still-`prepared` token to
`retired-precommit`, and unlinks the marker last. A new bridge attempt requires
a new operation ID and generation; the retired generation is first written to
an immutable digest-addressed archive chain. `commit-intent` and `consumed`
generations can never be retired or rearmed.

## Current ordinary deployments

The ordinary deployment commands remain prohibited until the bootstrap-router authority is complete and binds the exact required snapshot authority.

The manually adopted production runtime uses descriptor v4 and current-state
v3 for ordinary deployments. The prerequisite authority, immutable root Git
permission authority, source-successor authority, schema-v2 Worker-unit
authority, and exact least-privilege mutable-data audit role described above
must all be complete before this sequence starts. Every attempt uses one
unique lowercase operation ID and the full 40-character SHA currently at
`origin/main`:

```bash
deploy_operation_id=deploy-<utc-timestamp>

/usr/bin/python3 -I -B ./scripts/pull_deploy_controller.py plan \
  --sha <full-main-sha> \
  --operation-id "$deploy_operation_id"

nexpoly-pull-deploy prepare \
  --sha <full-main-sha> \
  --operation-id "$deploy_operation_id"
```

The bootstrap-router transaction has already installed the exact target
control release, but that minimal release intentionally has no complete target
Git object database. Therefore the first `plan` above is the sole direct
checkout exception: run it only from the exact private, standalone, clean,
complete-history, source-pinned T clone. It is read-only and validates the
active slot, source, active control, prerequisite source/CI, adoption
provenance, the immutable snapshot → old-root → source-successor → unit-
permission → router chain, and its schema-compatible hardened marker. Review
`authority_kind=manual-runtime-adoption`, all raw authority digests, the
permission marker/evidence/inventory digests, and the schema-v5 prerequisite
target-binding digest in its output. The stable selector explicitly rejects
this first `plan` so it cannot silently fall back to A or attempt an incomplete
proof from the installed release.

From `prepare` onward, never invoke a mutating or recovery verb from checkout.
The installed launcher first verifies the golden snapshot byte-for-byte, then
routes the sealed target to T before the old active controller can fetch. The
target controller creates the candidate state and independently revalidates
the permission wrapper, raw marker, and all eight lineage anchors;
compatibility with the old installed controller's marker parser alone is not
sufficient. Resume and apply pre-switch repeat the same exact binding. After
the first deployment writes current-state v3 and activates the target
controls, the one-time route retires and later releases continue using the
same installed command:

```bash
nexpoly-pull-deploy plan \
  --sha <later-full-main-sha> \
  --operation-id <later-deploy-operation-id>
```

The installed `plan` and `prepare` do not interrupt serving traffic. `prepare` must finish
before the maintenance window. It verifies the protected-main candidate and CI
checks, resolves image digests and labels, validates assets and migrations,
downloads locked wheels, builds the inactive MD Worker environment directly at
its final A/B slot path, and builds the immutable DFT environment at
`worker-venvs/dft/<full-main-sha>` without reading or changing either active
Worker runtime.

After `prepare`, rehearse the descriptor's exact PostgreSQL 16 restore and
0013→0015 transition while production remains online. Run the target script
from the same private, clean, source-pinned clone. First review the read-only
plan, then repeat every confirmation emitted by that plan:

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

The rehearsal creates a fresh custom dump, performs a network-isolated
PostgreSQL 16 restore, applies exactly 0014 followed by 0015 in the candidate
Backend image, and verifies the ledger, 615,159 property records, snapshot,
indexes, and query plans. Backup plus restore must finish within 30 minutes and
the two migrations within 10 minutes; the migration session proves
`lock_timeout=30s` and `statement_timeout=15min`. Its terminal sealed authority
is bound to the prepared descriptor and ready record. Ordinary `apply` refuses
to begin its business mutation unless it can load and validate that exact
report.

Enter the authorized maintenance window only after the rehearsal passes:

```bash
nexpoly-pull-deploy apply \
  --sha <full-main-sha> \
  --operation-id "$deploy_operation_id"
```

An ordinary `apply` obtains the exclusive deployment lock and then:

1. Revalidates and consumes the exact rehearsal authority before recording a
   mutation marker or changing business state.
2. Disables public writes and both task-submission paths, enables Backend and
   Worker drain, and proves MD and DFT both have zero running and queued jobs.
3. Isolates public ingress and stops Backend, Web, MD Worker and DFT Worker.
   Before the first stop call it durably seals the PostgreSQL container ID,
   image ID, named data volume and `pg_control_system().system_identifier`.
   It proves both Worker MainPIDs are zero, their sockets are gone, and no
   process still reads the live checkout. PostgreSQL remains running.
4. Creates and fsyncs the drain-final PostgreSQL backup, proves its isolated
   PostgreSQL 16 recovery, and binds that exact evidence.
5. Records the previous source SHA, tree, image digests, asset pointer, MD slot,
   DFT runtime/environment, and both tracked systemd units.
6. Fetches again, revalidates the target and fast-forwards the production
   checkout to that exact `origin/main` SHA.
7. Verifies HEAD, tree hash, remote identity and clean worktree before running
   target code.
8. Uses the recorded image digests, applies only the exact ordered 0014/0015
   migration transition, and runs strict schema preflight.
9. Atomically switches the prepared MD slot, DFT runtime/env/unit, MD unit, and
   application images. It starts DFT, MD, Backend, and Web/entry in that order;
   Backend uses
   `compose up --no-deps backend`; the sealed PostgreSQL container is never an
   `up` target and its full identity is rechecked before and after startup.
10. Verifies the candidate runtime and atomically writes current-state v3, but
    leaves public ingress and both submission paths closed. The operation
    remains durably staged at `awaiting-acceptance`; `apply` never opens it.

All processes that import or execute checkout files are stopped before the Git
working tree changes. Updating a running source tree in place is forbidden.

Acceptance is deliberately two-step. The first invocation starts a private
loopback-only candidate endpoint, runs the sealed DFT/MD/API/UI probes, cleans
up that endpoint and re-drains the candidate. The probes include six-model DFT
warmup and a minimum single-point calculation; MD one-running/two-queued
capacity, fourth-submit 429 and cancellation; property histogram and 2D
structure queries; knowledge; and the main frontend routes. Only after every
probe passes does the controller durably start the 900-second maintenance
observation:

Before the first probe command, the controller proves ingress isolation and
persists an `acceptance_probe_intent` containing the candidate-bound full
mutable-data digest. Failed or crashed probes may retain partial MD/DFT history,
so retries reuse that intent instead of incorrectly requiring the original
live row digest. A passing report is accepted only when a fresh post-probe
snapshot proves that PostgreSQL/system/role/ledger identity, schemas, static
and analytics data, migration exceptions, sequence structure and all
non-probe business data stayed unchanged. The new post-probe digest then
becomes the exact database baseline for the observation window.

```bash
nexpoly-pull-deploy accept \
  --sha <full-main-sha> \
  --operation-id "$deploy_operation_id"
```

The first `accept` returns `status=maintenance-observation`, keeps public
admission closed, and reports `acceptance_not_before`. Do not substitute the
earlier staging timestamp for this post-probe deadline. After that time, invoke
the exact same command a second time. This final invocation uses a read-only
runtime verifier and does not submit another MD/GPU canary. It revalidates the
sealed probe report plus source, current-state, PostgreSQL, runtime, Worker
fence, image and post-probe mutable-data identities. Only the current
operation's drain reason/timestamps/content digest are normalised; business
rows and sequences remain exact. In DFT `observe` mode, a fresh valid guard may
move between `ready` and `quarantined` without becoming runtime drift, while
the guard schema/GPU UUID/timestamp freshness, runtime readiness and process
identity are still independently required. It opens public ingress and
submissions only if those identities are unchanged:

```bash
nexpoly-pull-deploy accept \
  --sha <full-main-sha> \
  --operation-id "$deploy_operation_id"
```

Before `acceptance_resume_intent` is persisted, any failed acceptance probe,
changed identity, missing/stale authority, or maintenance-observation anomaly
is a stop condition. Admission remains closed; use the exact operation's
explicit rollback instead of opening services by hand. A stopped runtime may
be restarted only while still at the initial `awaiting-acceptance` boundary.
Once post-probe observation has started it is rejected without restarting or
reusing probes. After the sticky resume intent, rollback is forbidden and only
forward recovery is permitted. Recovery first read-only verifies the exact
candidate state/source, sealed probe authority and non-mutable database
provenance. A fully open exact public runtime is terminalized in place without
isolating ingress or reading mutable rows. A partial persistent resume is
isolated and exactly re-drained, then the candidate, runtime, non-mutable
database and Worker-fence identities are compared before the same runtime is
resumed. Writes accepted after an unknown resume commit are therefore
preserved. A stopped or drifted runtime keeps the sticky resume phase and
requires a forward fix; it is never restarted, reprobed or made rollbackable.
For both full-open and partial recovery, the top-level full runtime-verification
and Worker-fence digests must bind the sealed acceptance evidence. The freshly
read complete repository identity, including Git trust and permission-takeover
evidence, must exactly match the repository identity sealed in that runtime
verification. Re-drain recovery evidence cannot replace this full authority.

## Historical migrations and first takeover

This section applies to the retired takeover/bridge sequence documented above,
not to the current manually adopted ordinary-deployment path.

An empty database uses the complete bootstrap mode. The first takeover of the
existing production database uses the governed bootstrap-expand path and stops
before a trailing contract migration.

Destructive migrations are separate, checksum-pinned maintenance operations.
They require their own operation ID, drain, backup, isolated restore proof,
approval record, epoch barrier and rollback floor. A normal source deployment
must never infer or execute a pending contract. See
`docs/postgres-migration-governance.md`.

The first governed deployment must additionally prove the legacy runtime
identity and install and seal every bootstrap/recovery executable plus the
source-read-only external-database audit helper and builder listed above. If the production
ledger, registered database inventory, asset identity or rollback evidence
differs from the reviewed plan, the operation stops before mutation.

The frozen schema-v2 asset manifest is
`sha256:0588cc6a9acd50efbcba49850bbea79ab44fa1752fa530b8537ccb21753ebc9b`
and its only accepted predecessor is
`sha256:ad19a4f1cb954b3ee6999b7157c798fd887ecd3fd7ae12e40ac20a97637575e2`.
The controller re-hashes both releases and requires the `model`, `database`
and `backend-data` inventories to match the predecessor byte-for-byte. Only
`byteff2` may differ. Its release input declares an empty
`datasets_on_asset_change`; switching this asset must not invoke a database
import or rebuild. The retired controller rebuild entrypoint fails closed, and
the standalone static importer excludes every business-mutable table.

> **First-deployment stop condition:** control bootstrap and bridge preparation
> alone are permitted. Until the dedicated reconciliation control has backed
> up, restore-tested and removed exactly `0005_polytao_jobs`, operators must not
> run the first production `bridge-apply`, ordinary `apply`, or
> `bootstrap-expand`.

## Rollback and interrupted attempts

Rollback is explicit while the descriptor-v4 candidate is still staged with
public ingress and submissions closed:

```bash
nexpoly-pull-deploy rollback \
  --operation-id deploy-<utc-timestamp>
```

The controller stops candidate services and restores the previous source SHA,
image digests, asset pointer, MD slot/unit, and DFT runtime/env/unit before it
can run old-runtime smokes. For the current 0013→0015 deployment, reaching the
database migration phase is an unconditional whole-database rollback boundary:
regardless of the apparent transaction result, rollback must restore the
operation's verified drain-final post-0013 backup before the previous runtime
can start or accept writes. Before that phase, rollback may restore the sealed
source, images, assets, and Worker identities without replacing PostgreSQL.
Ingress and submissions remain closed until the old source, database, MD/DFT
runtime, and service identities all pass their recovery checks.

Once acceptance has completed and public admission has reopened, an ordinary
descriptor-v4 deployment is terminal and the same `rollback` command is
fail-closed before it writes a marker, takes a backup, drains, or stops any
service. Operators must forward-fix. The controller must never run the old
`fc05` source against the post-0015 ledger, and restoring the drain-final
post-0013 dump after reopening traffic would discard all intervening writes.
That data-loss operation requires a separately reviewed and authorized
maintenance entrypoint; ordinary `rollback` intentionally exposes no bypass or
confirmation flag.

Immediately before final admission resume, the marker seals a sticky
`acceptance_resume_intent` for the exact operation and candidate state. A lost
resume response may mean public writes were already accepted, so
`acceptance-resume-started` can only recover forward. New retries retain that
phase when the runtime is stopped or its identity drifts. A pre-existing
`acceptance-rejected` marker that already contains the sticky intent remains
loadable but requires a separately reviewed forward fix; neither automatic
convergence nor explicit rollback may restore its database.

The inverse transition is fenced as well. When a pre-intent staged rollback
has restored the old database, effects, and current-state authority, the
controller persists `rollback_admission_resume_intent` before reopening the
old runtime, then records `rollback-admission-resumed` only after resume
returns. That intent binds the failed candidate, previous deployment/adoption,
backup and restore evidence, and exact runtime fence. A crash in this window
can only verify an already-open old runtime or re-drain and resume that same
runtime forward; it must never repeat the database restore, so writes accepted
after an unknown resume commit are preserved.

The deployment marker and journal are stored below
`nexpoly-runtime/state`. An interrupted or ambiguous operation fails closed;
the operator must run the matching recovery or rollback command under the same
deployment lock. Never delete the marker, edit the migration ledger, run
`git clean`, or start services manually to bypass recovery.

### Historical bridge rollback details

For a failed first bootstrap/bridge, rollback order is fixed: the parent
controller first CAS-restores the sealed legacy main SHA/tree and HTTPS return
authority, then invokes takeover restore from the independent recovery
launcher, and only then starts the legacy runtime. Takeover restore accepts a
changed Worker unit or control layout only when their exact replacement
digests were sealed by the parent operation. The parent passes its already-held
`state/deploy.lock` as an authenticated inherited file descriptor; the child
verifies the same inode, direct-parent lock record and inherited open-file
description before taking the takeover execution lock. Operators never select
that descriptor or supply evidence digests manually.

Takeover restore is phase-journaled: after the parent returns the old Git
SHA/tree it CAS-restores every sealed checkout/`.git` permission deepest-first,
then restores classified paths, the exact prior controls/audit/backup layout,
the prior Worker unit and finally the sealed containers/user unit. The current
permission and control-layout replacement digests are computed from the private
takeover operation inventory; callers cannot choose the path set. A crash after
detaching a path may resume only when the remaining trash is an unmodified
subset of the original recursive seal. The terminal status must bind
`restored_terminal_sha256`; otherwise ingress remains isolated.

The controller may perform a controlled checkout of the recorded previous SHA
only in the dedicated production checkout. That authority never applies to a
development worktree, DFT worktree or AIMNet source tree.

## GPU services

GPU Broker and MPS units read code from the stopped-and-verified production
checkout and keep all writable state under
`/data/lzq/gith/nexpoly-runtime/state/gpu-resource`. They remain disabled in
production.

Production DFT runs directly on physical GPU2 with no Broker, MPS or overflow.
Each release has one immutable Python runtime below
`worker-venvs/dft/<release-sha>`. The runtime builder writes the exact active
paths, release SHA and runtime-manifest digest to the owner-private
`config/monomer-dft-runtime.env`; the tracked systemd unit consumes that file.
The active runtime must contain the six locked AIMNet models at mode `0600` and
must not contain or reference a compatibility launcher.

Production pins `NEXPOLY_DFT_GPU_GUARD_MODE=observe`; the software default
remains fail-closed `enforce`. In observe mode, the periodic GPU2 inventory
continues to validate and report `gpu_guard_mode`, `gpu_guard_status`, and
`gpu_contention_observed`, but an unknown external GPU2 process is a structured
warning only. A `quarantined` observation does not make DFT
`available=false`, `runtime_ready=false`, or stop task admission/execution, and
the deployment does not kill, migrate, or allowlist the external process.
Missing, stale, or invalid observations during service operation also warn
rather than disable the service; deployment readiness still requires a
well-formed, current observation with the correct GPU UUID and permits
`quarantined`.

This is deliberately best-effort availability, not GPU isolation. Contention
can still cause memory pressure, OOM, timeout, or CUDA failures. DFT remains
fixed at one executing job plus eight queued jobs
(`MONOMER_DFT_MAX_CONCURRENT_JOBS=1`,
`MONOMER_DFT_MAX_QUEUED_JOBS=8`). MD independently uses
`MONOMER_MD_MAX_ACTIVE_JOBS=3` as the total running-plus-queued admission cap
and `MONOMER_MD_MAX_CONCURRENT_JOBS=1` as the execution cap. Consequently MD
admits exactly one running job and two queued jobs; a fourth active submission
returns 429. `MAX_ACTIVE_JOBS=3` never means three simultaneous MD executions.

`production_readiness.py --dft-live-only` verifies the installed unit against
the tracked unit, the release-bound runtime environment, six-model warmup,
GPU2 UUID and guard state, immutable Backend/Web revisions, and that the
Backend was rendered only from the two tracked production Compose files.

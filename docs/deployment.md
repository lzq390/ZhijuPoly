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
ordinary untracked paths, and no ignored paths. Before the first takeover,
inventory every existing ignored path with its hash, owner and mode, and place
the exact reviewed classification in the private bootstrap input. The
source-pinned legacy takeover controller—not an operator shell command—moves
only that sealed inventory to its fixed runtime, secret and asset roots. Never
use `git clean` or manually move a classified path to satisfy this gate.
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
    ├── dft-a/
    └── dft-b/
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

## Operator workflow

The stable entry point is installed outside the checkout:

```text
/data/lzq/gith/nexpoly-runtime/bin/nexpoly-pull-deploy
```

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
  `nexpoly-postgres-media-evidence` builder, private schema-v2 registry and
  registry-pinned private `pg_service.conf` digest and PG16 image. It
  enumerates arbitrary-named PostgreSQL volumes, PGDATA
  bind mounts, both fixed private backup roots and the independent dev/health
  stacks. Dormant media are copied before PostgreSQL starts; logical backups
  restore only into network-none disposable clusters.
- the reviewed, non-secret
  `ops/config/postgres-media-audit-role.sql.example` contract for the online
  audit users. Provision their login secrets separately.
- a reviewed, non-secret
  `ops/config/mutable-data-audit-role.sql.example` provisioning/check
  contract. Run it as the cluster role administrator, connected to `nexpoly`
  in the maintenance window; provision the `nexpoly_mutable_audit` password
  out of band and install only the mode-`0600` pgpass value. The helper's
  schema-v4 evidence rejects any role attribute, membership, ownership or
  persistent write authority outside the exact `pg_read_all_data` contract.
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
tag or 90-day artifact is an authority. Then seal and apply the legacy takeover
using only the installed recovery launcher and the classification digest from
the install manifest:

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
  --apply \
  --production-root /data/lzq/gith/nexpoly \
  --runtime-root /data/lzq/gith/nexpoly-runtime \
  --confirm-production-root /data/lzq/gith/nexpoly \
  --confirm-runtime-root /data/lzq/gith/nexpoly-runtime \
  --confirm-source-tree <40-character-tree> \
  --confirm-worker-unit-sha256 sha256:<64-lowercase-hex>
```

Bootstrap consumes the already-installed takeover operation and fence from the
private runtime state; callers do not supply their digests. It atomically
installs F controls, replaces the exact mode-`0664` Worker unit under CAS, runs
`daemon-reload`, and records the takeover authority. Never pre-`chmod`, replace
or manually reload that unit. Remove the temporary bootstrap source only after
the installed immutable inventory and completed bootstrap authority verify.

### One-time production ledger-alias gate

The audited legacy production ledger contains one duplicate historical alias,
`0005_polytao_jobs`. The first full Pull deployment is deliberately blocked
until the fixed-purpose reconciliation control has removed exactly that ledger
row. Bootstrap installs this control outside the still-legacy checkout. Complete
Pull `plan` and `prepare` while the current production runtime is still online,
then enter the maintenance window, reconcile the alias, and run the already
prepared Pull `apply`. Once an alias operation marker exists but is incomplete,
all Pull commands remain blocked until that same alias operation recovers. Do
not update the production checkout first.

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

Every attempt uses a unique lowercase operation ID and the full 40-character
SHA currently at `origin/main`:

```bash
nexpoly-pull-deploy plan \
  --sha <main-sha> \
  --operation-id deploy-<utc-timestamp>

nexpoly-pull-deploy prepare \
  --sha <main-sha> \
  --operation-id deploy-<utc-timestamp>

nexpoly-pull-deploy apply \
  --sha <main-sha> \
  --operation-id deploy-<utc-timestamp>
```

`plan` and `prepare` do not interrupt serving traffic. `prepare` must finish
before the maintenance window. It verifies the protected-main candidate and CI
checks, resolves image digests and labels, validates assets and migrations,
downloads locked wheels, and builds the inactive Worker environment directly
at its final A/B slot path.

The first governed takeover is the sole exception to “deploy current main”.
After crash-safe legacy takeover has produced a clean SSH checkout and
bootstrap has installed the current F control plane, the operator supplies
only F—not a historical target—to the bridge commands:

```bash
nexpoly-pull-deploy bridge-plan \
  --authority-sha <full-F-sha> \
  --operation-id bridge-<utc-timestamp>

nexpoly-pull-deploy bridge-prepare \
  --authority-sha <full-F-sha> \
  --operation-id bridge-<utc-timestamp>

nexpoly-pull-deploy bridge-apply \
  --authority-sha <full-F-sha> \
  --operation-id bridge-<utc-timestamp>
```

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

`apply` obtains the exclusive deployment lock and then:

1. Enables Backend and Worker drain and waits for all active work to finish.
2. Isolates public ingress and stops Backend, Web, MD Worker and DFT Worker.
   Before the first stop call it durably seals the PostgreSQL container ID,
   image ID, named data volume and `pg_control_system().system_identifier`.
   PostgreSQL remains running.
3. Creates a private PostgreSQL backup and proves it can be restored in an
   isolated PostgreSQL 16 instance.
4. Records the previous source SHA, tree, image digests, asset pointer and
   active Worker slots.
5. Fetches again, revalidates the target and fast-forwards the production
   checkout to that exact `origin/main` SHA.
6. Verifies HEAD, tree hash, remote identity and clean worktree before running
   target code.
7. Pulls the recorded image digests, applies only allowed expand migrations and
   runs strict schema preflight.
8. Switches the prepared A/B slots and starts Workers and Backend with
   `compose up --no-deps backend`; the sealed PostgreSQL container is never an
   `up` target and its full identity is rechecked before and after startup.
   It then starts Web and runs required model, database, API, UI and
   calculation smokes.
9. Writes the successful deployment state atomically before restoring ingress.

All processes that import or execute checkout files are stopped before the Git
working tree changes. Updating a running source tree in place is forbidden.

## Migrations and first takeover

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
`sha256:e5088b7954f7ee8f6cc4e45af36761fdc44d2fc374643441fe07283475de06c8`
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

Rollback is explicit:

```bash
nexpoly-pull-deploy rollback \
  --operation-id deploy-<utc-timestamp>
```

The controller stops candidate services, restores the previous source SHA,
image digests, asset pointer and Worker slots, and runs the old runtime smokes
before restoring ingress. Compatible expand migrations may remain. A database
change that is not backward-compatible requires restoration from the verified
backup before the previous runtime can accept writes.

The deployment marker and journal are stored below
`nexpoly-runtime/state`. An interrupted or ambiguous operation fails closed;
the operator must run the matching recovery or rollback command under the same
deployment lock. Never delete the marker, edit the migration ledger, run
`git clean`, or start services manually to bypass recovery.

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
`/data/lzq/gith/nexpoly-runtime/state/gpu-resource`. They are installed as
disabled capabilities. Enabling MPS/Broker, enabling a production DFT Worker,
and opening DFT admission are distinct reviewed maintenance changes; a normal
pull deployment does not enable them.

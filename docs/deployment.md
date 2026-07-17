# NexPoly production deployment

Production is updated by a reviewed, commit-pinned pull on the server. GitHub
Actions validates every candidate and publishes the Backend and Web images, but
it never contacts or mutates the production host. An operator runs the stable
deployment command during an authorized maintenance window.

## Filesystem boundaries

The source checkout is fixed at:

```text
/data/lzq/gith/nexpoly
```

It must track `origin/main` and be completely clean: no tracked changes, no
ordinary untracked paths, and no ignored paths. Before the first takeover,
inventory every existing ignored path with its hash, owner and mode, then move
secrets, models, caches, journals and results to the runtime root below. Verify
the external pointers and configuration after the move. Never use `git clean`
to satisfy this gate. Runtime state is never stored in that checkout:

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

All eight executables are fixed-name, deploy-user-owned mode `0700` files. The
deployment descriptor seals every hash. The status helper is read-only; the
unchanged-resume helper may restore ingress only and must prove the Backend and
Worker processes did not restart; the full restore helper is reserved for a
runtime already stopped or partially replaced. Each helper must be idempotent
across a lost response. There are no configurable shell-command selector
variables.

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
the non-mutating plan:

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

Bootstrap itself backs up the exact existing mode-`0664` Worker unit, makes it
private, atomically replaces the pathname with a new verified inode, runs
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

After reviewing the exact cluster, ledger, nine-row archive, PostgreSQL client,
backup and isolated-restore plan, repeat the same operation ID with the explicit
write confirmation during a maintenance window. Isolate ingress; stop the
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

`apply` obtains the exclusive deployment lock and then:

1. Enables Backend and Worker drain and waits for all active work to finish.
2. Isolates public ingress and stops Backend, Web, MD Worker and DFT Worker.
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
8. Switches the prepared A/B slots, starts Workers, Backend and Web, and runs
   required model, database, API, UI and calculation smokes.
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
identity and install and seal all eight bootstrap executables listed above. If
the production ledger, registered database inventory, asset identity or
rollback evidence differs from the reviewed plan, the operation stops before
mutation.

> **First-deployment stop condition:** control bootstrap alone is permitted.
> Until the dedicated reconciliation control has backed up, restore-tested and
> removed exactly `0005_polytao_jobs`, operators must not run the first
> production `apply` or `bootstrap-expand`.

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

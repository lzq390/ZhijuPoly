# Production readiness authority

`nexpoly-production-readiness` is the final, read-only admission check for one
exact final-main authority (`F`) and one exact historical bridge (`B`). It
aggregates evidence; it does not deploy anything.

The command has deliberately no collection or repair code. In particular, it
does not fetch Git, pull images, run Compose, start or stop containers, invoke
systemd, contact PostgreSQL, change the live asset pointer, or create/update a
runtime marker. A separately reviewed read-only collector must seal its
observations into:

```text
/data/lzq/gith/nexpoly-runtime/audit/production-readiness/evidence.json
```

The runtime directory is mode `0700`; the evidence file must be an
owner-matching, non-symlink regular file with exact mode `0600`. Live evidence
expires after 15 minutes. The command reads each file through `O_NOFOLLOW`,
checks the inode before and after the read, and rejects unknown or missing JSON
fields.

## Collector provisioning and capture

The repository ships
`ops/config/production-readiness-collector.example` as a fail-closed interface,
not as a usable collector. A site review must replace its marked body in the
private bootstrap-input staging directory. The prerequisite installer rejects
the unchanged template, installs only the reviewed implementation as
`runtime/config/production-readiness-collector` mode `0700`, and records both
the collector template and the mutable-data audit template in the
source-pinned takeover install manifest. The collector is a site-specific
read-only helper, but it is deliberately not a `site_helper_contracts`
evidence helper: only `production_readiness.py` validates its complete closed
evidence envelope.

The private implementation must preserve the exact invocation surface:

```bash
/data/lzq/gith/nexpoly-runtime/config/production-readiness-collector \
  --authority <exact-F-SHA> \
  --bridge <exact-B-SHA>
```

Both arguments are distinct full 40-character commit IDs. The collector first
samples the fixed SSH origin's `refs/heads/main`, then captures every other
section, and finally samples the same ref again. Both CAS samples must equal
exact `F`; the standalone clean source must still be exact `F`, while exact
`B` must remain the policy target and a strict ancestor. These are read-only
`ls-remote` observations: collection must not fetch, pull, update a ref, create
an object, switch a checkout, or mutate the source.

The collector has a closed read allowlist: owner-safe fixed files; local Git
object/status/fsck inspection; the two remote-main CAS samples; read-only
CI/OCI metadata requests; read-only capacity and process inspection; reviewed
Docker Engine GET-only endpoints; and PostgreSQL `SELECT`/`COPY` through the
dedicated audit role in one `REPEATABLE READ, READ ONLY, DEFERRABLE`
transaction after verifying `transaction_read_only`. Credentials come only
from pre-provisioned, owner-only mode-`0600` files and never appear in argv,
environment, output, or evidence.

It must not invoke Git fetch/pull/push, Docker or Podman CLI, Compose,
systemctl/systemd, a non-GET Docker endpoint, an image or container mutation,
or any database write/DDL. It must not write the production checkout, runtime
state/config, live asset pointer, unit, container, volume, image, database, or
another audit tree. Its only write is an owner-only mode-`0600` temporary file
inside the fixed owner-only mode-`0700`
`audit/production-readiness` directory, followed by file/directory `fsync` and
an atomic rename to `evidence.json`. The evidence publication itself is audit
output, not production state.

After collection, invoke the admission command below with the same exact F/B.
Do not hand-edit or partially merge section files: stale, unknown, missing, or
cross-authority fields fail the whole envelope. Live validation rehashes the
installed collector, preserves the aggregate helper-installation seal, and
requires that exact byte digest to equal `observation.collector_sha256`.

## Invocation

Use full 40-character commit IDs, never branches, tags, short SHAs, or mutable
image tags:

```bash
/data/lzq/gith/nexpoly-runtime/bin/nexpoly-production-readiness \
  --authority <exact-F-SHA> \
  --bridge <exact-B-SHA>
```

Success writes one sanitized JSON object to stdout and exits `0`. Any missing,
stale, contradictory, unsafe, or malformed evidence writes a generic
`not_ready` JSON object to stderr and exits `2`. The error includes only a
digest of the internal detail, so a path, DSN, token, database user, container
ID, volume name, or system identifier cannot leak through the command output.

The strict success-result JSON Schema is available without touching runtime
state:

```bash
/data/lzq/gith/nexpoly-runtime/bin/nexpoly-production-readiness \
  --print-output-schema
```

For CI and contract tests, `--offline-fixture PATH` runs the same schema and
cross-binding validation but skips freshness and live-path checks.

## Evidence envelope

Every section has an `evidence_sha256` over its canonical JSON excluding that
field. The top-level `evidence_sha256` seals all sections. Objects use exact
field sets; `additionalProperties` semantics are enforced in code.

The sections prove:

| Section | Required authority |
| --- | --- |
| `git` | Remote-main CAS before/after equals `F`; the local source is owner-private, clean, standalone, complete, SSH-only, and has no ignored, dangling, replacement, sparse, or special-index state; exact `B` is a strict ancestor and matches the F policy. |
| `ci` | Every policy-required job succeeded for exact `F`, with immutable workflow digest and run/attempt identity. |
| `oci` | F and B backend/web records separately bind digest ref, OCI index digest, platform digest, local Docker image ID, revision, and source version; the PG16 restore image is fixed. |
| `asset` | The inactive schema-v2 digest matches policy; predecessor and all tree digests match the fixed contract; the B0 builder archive proves B0→B→F ancestry; the live pointer is unchanged and database effect is `none`. |
| `prepared` | Descriptor schema v3, READY file, one-time bridge token, policy, prefetch identity, takeover binding, F authority, and B target are one operation. |
| `prefetch` | Exact F/B bundle, recovery tools, source-readiness, immutable images/wheels, and schema-v2 asset evidence are sealed. Live mode checks the existing READY identity without running the deep verifier that creates a temporary clone. |
| `helpers` | All reviewed and site-specific helper contracts are installed with an exact installation digest, and the command itself is executing from the active content-addressed F control release. |
| `takeover` | The crash-safe legacy takeover reached its completed terminal state under F, with no active operation marker. |
| `alias` | The persistent 0005 operation is completed and its backup, isolated PG16 restore, audit manifest, and production system identifier remain valid. |
| `external_media` | The registry is complete, both external databases and every registered dormant volume/backup were audited read-only, the writable target is production only, and no old-0013 medium requires 0014. |
| `postgres` | Running container, image, volume, system identifier, and ledger source are digest-bound; the system identifier remains the alias-gate identity and the probe was read-only. |
| `migrations` | The observed ledger is exactly one of frozen `pre-0012`, `post-0012`, or canonical `post-0013`; 0012/0013 flags and manifest digest agree. |
| `mutable_data` | A schema-v4, repeatable-read, read-only/deferrable snapshot seals every business table (including `online_knowledge.jobs/history`), static table, PG runtime identity, row count, and content digest against the same PG and migration ledger. |
| `native_runtime` | Python 3.12, uv/build lock, wheel inventory/RECORD, clean AIMNet archive, model digests, F image/tree GPU report, GPU1/3 use, and no production GPU2 contact. |
| `capacity` | Available disk/memory meet the sealed requirements; disk requirements cover wheel cache, schema-v2 release, and backup reserve. |
| `conflicts` | Deploy, 0012 contract, alias, takeover, bridge, prepared-operation, and control-handoff conflict sets are all empty. |
| `observation` | Evidence collection used no fetch, pull, container/service mutation, or state write, and database observation was transaction-read-only. |

Live mode additionally revalidates the prepared descriptor and READY file,
bridge token, prefetch READY seal, helper installation, completed takeover,
completed alias gate, external-media audit, and the global deploy/contract/
takeover conflict markers. It also revalidates the mutable-data snapshot and
binds its container, image, volume, system identifier, and ledger back to the
PostgreSQL section. These are reads only.

## Media CAS extension

The base contract always requires the complete external-media registry and
audit. The `external_media.cas` field is a stable extension point for the
separate media-CAS work:

- `null` is accepted while that implementation is not part of the frozen
  bridge; the sanitized result reports `cas_status: "not-present"`;
- once present, it is a closed, sealed object containing schema/status,
  registry, manifest, inventory, media-count, and evidence digests; it must
  report `ready` and match the base registry/count;
- unknown or partial CAS fields fail the entire check.

A maintenance policy that requires CAS can therefore additionally require
`external_media.cas_status == "ready"` without changing this evidence schema.

## Installation and release contract

Bootstrap installs the immutable wrapper into `runtime/bin`, creates
`audit/production-readiness` as `0700`, and publishes the implementation only
inside a content-addressed control release. The selector exposes the
`production-readiness` role. Maintenance prefetch includes the wrapper and
implementation in both the controller identity and F/B recovery-tool
inventories, so the command cannot silently drift after readers are stopped.

This command is an admission report only. A successful result does not
authorize the current task to mutate the independent production repository,
database, containers, units, images, or live asset pointer.

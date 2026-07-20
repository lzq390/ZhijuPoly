# Monomer-DFT integration provenance

This record explains how the historical Monomer-DFT work was converged into
the governed integration branch. It is provenance only: deployment authority
comes from the final bridge policy, exact Git trees, OCI digests, runtime
locks, and acceptance evidence.

## Preserved source history

The original component tips remain recoverable through
`refs/archive/premerge-20260717T090623Z/commit/<full-sha>` and the verified
pre-merge bundle:

| Component | Preserved tip | Tree |
|---|---|---|
| Combined integration hardening | `799129445cd135ef5a13f256242349da799356d9` | `a0daf029bd7aa38e47fdda18fbfccf8a59b855ef` |
| Backend | `f4a67f1c85b045859d26465a75bb5d46cbdf5ade` | `0c0f33e48450c7ed6e1d9c7b4355c78e0e5470f8` |
| Frontend | `d8a3fef2d529550127759b8872fffc21db2e2252` | `574a0065adb47e2738bd780254e263e1716566ed` |
| Worker | `ddc3ed089b23bc3ab433e9a49cfbae014ced5516` | `1f545cf518b222415bc910a153c7ca2b050ddf31` |
| GPU runtime | `05a6691d82978451fdfb09fff01cfbcfa51af360` | `5350841c14cb008fa8ff068e01b4edf8420427cb` |
| Operations/dev delivery | `3a182343910c4f2fcde159481d63082f86ef8472` | `13b05fbc6bafc6598f5daa34d4c51fedf69d2954` |
| First disabled integration | `447567e6b363d5274111c3dd55558954288d5b94` | `1afed8a0ab51d43cc968e9ba21a457d4226317a9` |
| Readiness-hardened integration | `2c0fb02b5213d866975659e86c4b9af7e6b61c22` | `ea082f72d61c36ba24e57b5c6b04b18873d40541` |
| Historical combined base | `6fce223f644d9558dd79028c1ba0499d33f4031d` | `89b5d0ef9d73c83db4ad561fdaac0babc194c2b8` |

The bundle is
`nexpoly-pre-merge-20260717T090623Z/nexpoly-all-refs.bundle`, with SHA-256
`af37b7af7879da30c120280c28abdc5338bc0f5ae77bd672c57ba0031ebae6ee`.
Its restore test verified all 90 archive refs and the recovered dropped stash.

## Dirty-worktree capture

The historical DFT worktree was captured before convergence with base
`6fce223f644d9558dd79028c1ba0499d33f4031d`. The private recovery set contains:

- `tracked-wip.patch`, SHA-256
  `78ee870f45a0b9b735091597e145d018f94ae3617db71dd0f1b77a7b1f0066b9`;
- `untracked-source.tar.zst`, SHA-256
  `b863b376aac5ee12ba8038bf7cf3634d6c5ae4a517ad82e881323882a893deb4`;
- the porcelain-v2 status, untracked inventory, and tar inventory used to
  prove the capture boundary;
- the earlier snapshot
  `refs/codex/snapshots/monomer-dft-20260715T023922Z` at
  `948eee234f188292200b4a7d965479211568a987`.

No `.env` file, credential, model checkpoint, run journal, or proprietary
result was imported into Git from that capture.

## Convergence into the governed tree

The component commits are intentionally not merge ancestors of the final
integration history. Their source, tests, and documented behavior were
transplanted onto current `origin/main`, then hardened in the following
ordered integration range:

- `a2cda12` shared application scaffolding;
- `4ba8410` backend domain and API implementation;
- `de050b6` Worker and frontend modules;
- `4bd7e22` frontend API integration;
- `1acc523` GPU result and lease fencing;
- `092c2c8` schema readiness and production hard-off;
- `831652a` migration-only database boundaries;
- `b73d7d9` isolated development delivery;
- `18edd51` development-only Worker controls;
- `5c0e139` canonical CI and bridge compatibility gates;
- `35f2c1b` controller/Worker contract reconciliation;
- `f49e184` final bridge/readiness contract closure;
- `8268fcf` exact transient-scope workload launch;
- `68c0683` protocol-safe MPS termination;
- `1733585` exact GPU acceptance evidence;
- `0c7cf33` formal acceptance authority sealing;
- `8cd7bfc` governed development lifecycle documentation;
- the final production-policy, migration-transition, systemd/MPS inventory,
  workflow, and provenance convergence changes committed on top of that
  ordered range.

The temporary `.github/workflows/monomer-dft-ci.yml` was deliberately
superseded by the canonical `ci-gate`; `backend/requirements-monomer-dft-ci.txt`
was superseded by the governed Worker/runtime lock. Omitting those two
temporary files is therefore intentional rather than an incomplete port.

The migration identity carried forward is
`0013_monomer_dft_jobs` with checksum
`ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc`.
The final tree must continue to enforce `schema_ready`, stable
`schema_not_ready` responses, production hard-off, development-only
Broker/MPS paths, and compatibility with existing `/api/v1/dft` behavior.

## Cleanup rule

Historical component branches and worktrees may be removed only after:

1. the final main tree has passed CPU, PostgreSQL 16, bridge, candidate GPU,
   final-main GPU, and legacy V1 journal-upgrade acceptance;
2. a second bundle has restored successfully in a temporary clone and passed
   `git fsck --full`;
3. the final branch/tree mapping and immutable OCI/runtime/asset identities
   have been recorded in deployment evidence.

Archive refs and Codex internal refs are not ordinary delivery branches and
remain preserved after branch cleanup.

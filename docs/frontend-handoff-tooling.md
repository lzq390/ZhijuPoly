# Frontend handoff tooling source layer

This source-only candidate contains the packaging runner, data-free page readiness
and cancellable event-stream helpers, and their boundary tests.
It preserves stable source capture, environment/private-file exclusions,
run ownership and resume checks, deterministic archives, manifest verification,
adaptation hooks, isolated frontend validation and the final metadata audit.
The source allowlist includes only `polymer_md/user_configuration.json` and
`polymer_md/capability_catalog.json` from the shared Polymer MD package. These
public UI specifications keep their relative import paths; executors, runtime
state and other files from that package remain excluded. Adapted browser service
fixtures delegate authentication to the isolated lower transport and retain
their explicit business failures.

## Asset and adapter dependency

The runner requires a separately reviewed `tools/frontend-handoff` kit under
the selected source root. That kit supplies the overlay, documentation adapter,
templates, fonts/licenses and application fixture assets. Only selected reviewed
source modules are included here. The complete adapter and asset kit is absent;
its download archives, scientific JSON/CSV data, asset generators or equivalent
payloads are not copied into this candidate. There is no automatic fetch, fallback
generator or path to the held historical worktrees.

Without the kit, the normal and `--prepare-only` entry points fail before
creating a packaging run. The source layer alone is not a complete or verified
frontend handoff. Supplying a kit later requires a separate content review and
the existing authority to use its assets. Source-layer tests are not approval
to redistribute data or publish an archive.

## Local checks and provenance

Run `python3 -m unittest discover -s scripts/tests -p test_frontend_handoff_package.py`
with the existing Node 22 runtime on PATH to exercise all JavaScript callbacks.
These tests use temporary synthetic archives and responses; they do not need
frontend dependency installation, a backend, database, GPU or scientific job.
The entire frontend dev/preview/pages/focus/states/motion validation remains a
separate end-to-end stage and has not been repeated for an absent adapter kit.

The locally reviewed packaging baseline retains the final-runtime-metadata
audit before release and all test methods from the older ignored prototype.
Detailed source commit/hash provenance is retained in the local review audit.
This candidate additionally validates adapter input types before source capture.
Already integrated proxy, query-guide, metering and DFT maintenance changes
are not reapplied.

## Data-free adapter checks

Run `node --experimental-strip-types --test tools/frontend-handoff/tests/readiness-and-stream.test.mjs`
with the existing Node 22 runtime. These checks retain the four original pure
readiness assertions and cover finite/live event ordering, cancellation,
pre-aborted signals, synchronous subscription abort and provider errors.
The stream releases subscriptions and abort listeners once on every covered
termination path. Original App/mock integration tests require the separate
scenario-provider dependency and are not replaced by these pure checks.

## Explicit scenario-provider composition

The Vite transport now receives `fixtureFactory` explicitly. It retains the
handoff CSP, fixed download mappings, HTTP session continuity, request/response
headers, cancellation and stream behavior. HTTP-fixture mode fails if no
provider is supplied; it does not fall back to a production API or fabricated
business success. The packager wires the external scenario bridge into its
generated Vite configuration.

`handoffAuthFixture.ts` is a separately testable, synthetic in-memory auth-only
handler. It preserves guest/member capabilities, demo login/logout and session
retirement on demo password changes. It never contacts a real auth service,
stores entered secrets or changes real accounts. Non-auth requests are left
to an explicit provider; this module is not the full scientific scenario mock.

The original full `handoffMockFetch.ts` contains scientific result construction
as well as control flow. It and its coupled scenario/App tests are not copied
into this data-free layer. They remain available in the held old worktrees.
The small bridge preserves that external provider interface, so a complete
handoff still needs separately reviewed provider and asset inputs. Preflight
checks their presence before snapshotting; those checks only inspect metadata.

## Generic documentation, archive checks and visual resources

The source layer retains staged documentation adaptation and browser-verifier
source. The adapter rejects source Git roots and symlinked stage/doc paths,
prepares changes before writes, preserves fenced examples and deactivates
out-of-stage links without reading their targets. It has eleven synthetic tests
and was also checked against the four current public main documents.

Archive inspection is a standalone helper, retaining CRC/size/compression checks
and rejecting traversal, Windows paths and NUL names. Its tests use temporary
unit-only ZIPs. The browser-verifier assertions remain in source; they were
syntax-checked, not executed against a complete application in this candidate.

Four public font files, their OFL texts and pinned source hashes, six explicit
placeholder SVGs and a local agent placeholder are ordinary visual resources.
Their inclusion does not include scientific rows, result downloads or generators.
Templates are retained with an explicit notice that they describe a future full
handoff, not a validated package currently available from this source layer.

Of the original 62 kit paths, 33 generic/ordinary-resource paths are integrated.
The 26 scientific data/generator paths and three full-Mock/App-coupled source/test
paths remain in the original worktrees. Only auth/session control and the four
pure readiness assertions were separated from the mixed scenario layer.
No complete dev/preview/browser matrix or scientific result was verified here.

# Frontend handoff tooling source layer

This source-only candidate contains the packaging runner and its boundary tests.
It preserves stable source capture, environment/private-file exclusions,
run ownership and resume checks, deterministic archives, manifest verification,
adaptation hooks, isolated frontend validation and the final metadata audit.

## Asset and adapter dependency

The runner requires a separately reviewed `tools/frontend-handoff` kit under
the selected source root. That kit supplies the overlay, documentation adapter,
templates, fonts/licenses and application fixture assets. None of that kit,
its download archives, scientific JSON/CSV data, asset generators or equivalent
payloads are copied into this candidate. There is no automatic fetch, fallback
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

# Handoff runtime and separate data dependencies

This repository currently contains the reviewed generic source layer and ordinary
licensed/placeholder visual resources. It does not contain the scientific
scenario provider, scientific rows, download payloads or data generators.

The Vite transport accepts `handoffPlugin({ httpFixtures, fixtureFactory })`.
HTTP-fixture mode requires an explicit provider. The packaging runner composes
the provider through `handoff-fixtures.mjs` only in a complete, separately
reviewed kit; no production API fallback or substitute scientific data is used.
The bridge still names the external scenario factory, which is intentionally
absent here. Preflight rejects the incomplete kit before creating a run.

Preserved generic behavior includes fixed filename download routes, local CSP,
session continuity, cancellation, response headers and explicit 501 responses.
Download routes require separately reviewed files in `public/handoff/downloads`.
The source checks do not establish those files' contents or scientific validity.

Keep server and preview proxies disabled in the eventual staged handoff. Apply
the overlay only to an isolated snapshot. Real dev/preview/browser matrices must
be repeated after all authorized provider and asset inputs are available.

The four local font files are unchanged public Google Fonts inputs, pinned in
`assets/public/fonts/sources.json`; their OFL license files accompany them.
SVG paper thumbnails and the agent page are explicit non-scientific placeholders.
Scientific dataset descriptions are dependency references, not included data.

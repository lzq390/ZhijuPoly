# Governed OpenScience UI overlay

This directory derives the production OpenScience UI from the exact image that
was running on 2026-08-20. The base had no source or revision labels, so the
overlay deliberately patches only its already-deployed NexPoly bridge resolver.

The effective static tree is allowed to differ only by replacing
`assets/index-B2eNxQLj.js` with a cache-busted patched bundle and updating
`index.html`. Upstream lazy chunks import the original entry URL, so the index
also installs an exact import map from that URL to the new content-addressed
entry before the first module script. This preserves the untouched upstream
chunk graph without executing or trusting a cached copy of the old entry. The
resolver selects the actual parent from `document.referrer` and accepts only
the production and development NexPoly origins. It never uses a wildcard
target origin.

The image labels bind both the baseline and derived static-tree manifests, the
baseline manifest/config identity, the two explicit parent Origins, and the
SHA-256 of their newline-delimited allowlist policy.

The raw image is retained privately for audit. The same manifest is also
mounted under the public, repository-bound `nexpoly-web` package with the
dedicated tag `openscience-base-e7d25a1b6d51`, allowing GitHub Actions to pull
it without storing a personal token. The overlay Dockerfile always addresses
that mirror by digest, never by tag. Any explicit `OPENSCIENCE_BASE_IMAGE`
override must remain a digest reference, and `OPENSCIENCE_BASE_MANIFEST` must
repeat the same manifest.

```bash
docker build \
  -f ops/openscience-ui-overlay/Dockerfile \
  --build-arg OPENSCIENCE_BASE_IMAGE=ghcr.io/lzq390/nexpoly-web@sha256:e7d25a1b6d515daec641c8de9c98265f275991eee2396dc578ce9c2fcfdeb197 \
  --build-arg OPENSCIENCE_BASE_MANIFEST=sha256:e7d25a1b6d515daec641c8de9c98265f275991eee2396dc578ce9c2fcfdeb197 \
  --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
  -t nexpoly-openscience-ui:local \
  .
```

Do not rebase this binary overlay onto another OpenScience image. A different
base requires a source-governed integration and a new reviewed patch contract.

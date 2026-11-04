# OpenScience static releases and browser caches

The production OpenScience UI on port `9011` is deployed separately from the
NexPoly web application. Preserve the current UI image and its API routes when
fixing static delivery; the historical `ops/openscience-ui-overlay` image uses a
different, older UI base and is not a replacement for the current application.

Old browser documents can still request the hashed JavaScript entry from their
release. Deleting that entry during an update leaves those documents blank even
when the new document and its resources are healthy. HTML cache revalidation
prevents new stale documents, while retaining complete old resource graphs lets
already open or cached documents continue loading.

## Prepare an immutable release

Record the deployed UI image digest, container configuration and static tree.
Export its `/usr/share/nginx/html` and `/etc/nginx/nginx.conf` into an operation
directory. Export retained prior release trees from their fixed images as well.
Do not read backend credentials or modify live container files to prepare a
release. Keep previous JavaScript bytes and filenames unchanged: an old entry is
not an alias for the latest entry, whose exports and dependencies may differ.

Run the repository preparation helper with the current tree and each retained
previous tree. The output must be a new directory. Include the resource known to
be requested by an older browser as a required asset; the helper verifies the
resource closure rather than copying only that one file.

```bash
python3 scripts/prepare_openscience_static_release.py \
  --current-root /operation/current-html \
  --previous-root /operation/previous-html \
  --output-root /operation/build/static \
  --manifest /operation/static-manifest.json \
  --required-asset assets/index-nexpoly-6ec6a90395a8.js
node ops/openscience-static-release/patch-nginx-cache.mjs \
  /operation/current-nginx.conf /operation/build/nginx.conf
```

The Nginx patcher changes only the reviewed static locations and refuses unknown
directives. It preserves the current listener, API upstreams and parent bridge
policy. `/index.html`, SPA fallback routes and mutable root resources revalidate
with `Cache-Control: no-cache, must-revalidate`. Successful `/assets/` responses
use a one-year immutable policy; missing assets return `404` with `no-store`.
Reapplying the same policy validates it without adding duplicate locations.

Build the thin release layer with `ops/openscience-static-release/Dockerfile`,
passing `OPENSCIENCE_CURRENT_IMAGE` as the recorded current image reference with
its digest and `SOURCE_REVISION` as the reviewed repository commit. Its build
context is `/operation/build`, containing only `static/` and `nginx.conf`. The
layer copies the prepared static tree and Nginx configuration onto that same UI
base. Retain every prior asset needed by supported cached documents in subsequent
releases; do not drop the previous release merely because its own deployment has
ended. Conflicting bytes at one asset path must fail preparation.

## Verify and release

Run the static preparation tests and
`node --test ops/openscience-static-release/test_patch_nginx_cache.mjs`. Validate
the candidate with `nginx -t` and an isolated listener. Verify root and SPA HTML
revalidation, mutable bootstrap resources, immutable current and retained assets,
uncached missing-asset responses, and complete old-entry module imports in a
browser. Check both actual parent pages and their history sidebars, including
project and general sessions. A `200` from the entry alone does not prove its
imports or the bridge work.

Run `node ops/openscience-static-release/verify-cache.mjs <candidate-url>` with
each retained asset path as an additional argument to verify HTTP cache headers.
The existing governed overlay image and browser CI scripts also exercise this
shared cache policy; its static-tree and bridge contracts remain unchanged.

Deploy only the UI service through its existing Compose project with the reviewed
candidate image. Preserve backend containers, worker services, network membership,
port bindings and the public-ingress firewall. Compare their identities and
firewall metadata before and after the switch. Keep the previous image for
rollback and record the static manifest and candidate verification evidence.

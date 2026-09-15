# Ketcher 3.8.0 / NexPoly r4

These patches apply to the published 3.8.0 packages, including the macro editor
bundled in ketcher-react's lazy ESM/CJS chunks. They preserve the upstream license.

- Lifecycle work references https://github.com/epam/ketcher/pull/9785 and
  https://github.com/epam/ketcher/pull/11555. In addition, NexPoly tracks all actual
  DOM callbacks and uses per-initialization roots, disposers and cancellation.
- Standalone owns one Worker per service and queues all 14 request methods. The
  embedded Worker/WASM bytes and message protocol are unchanged. Chemical failures
  reject one task; timeout, protocol faults and destruction retire the service.
- SDK async resumes check their captured owner; teardown fences writes before
  releasing React resources. Macro store/global cleanup checks instance identity.
- Macro initialization and menu updates run in effects. Children wait for their
  instance ID before rendering. Browser process compatibility lives in build/.
- Stale sourceMappingURL trailers are removed from changed JS. Standalone exports
  the official declaration file through exports.types; peer declarations are intact.

`npm ci` applies these patches and verifies package versions plus SHA-256 digests.
Builds verify again without changing installed files. A failed patch/hash check
must fail CI. Never use `--force`, `--legacy-peer-deps` or alter the manifest merely
to bypass a failure. Miew and Draft peer warnings remain subject to browser gates.

When updating a patch, review both module formats and the bundled macro copies,
regenerate the patch against pristine npm artifacts, update ketcher-manifest.json
with the reviewed artifacts' hashes, and rerun Worker tests and both browser gates.
The manifest contains original and patched digests for reproducible comparison.
A passing build alone does not authorize changing the default application engine.

The toolbar intersection refs also used state setters as DOM ref callbacks. In
React 19, clearing those refs during nested-root unmount schedules a second empty
commit; Fast Refresh's root registry then retains the retired SDK. The patched
local IntersectionObservers disconnect without setting state on ref removal.
Resize observers similarly cancel trailing work and ignore detached containers.
The production and development probes check weak references as well as counts.

Revision r2 also checks the actual small-editor element visibility, so its keyboard/clipboard callbacks stay inactive when the macro view is open.

Revision r3 also scopes the macro UI help shortcut to its visible canvas and owned portals. Hidden macro views no longer intercept the host page question-mark shortcut; the browser proof verifies that no popup opens and the host event remains unmodified.

Revision r4 adds deferred Macro capability and a production SDK delivery boundary.
`Editor.deferMacromoleculesEditor` defaults to false; the product enables it.
`ensureMacroReady()` prepares capability without switching views or replacing the
small-molecule document. Imports, formatters, exports and mode changes use the
same per-instance controller. Mode methods return promises after model conversion
and the matching UI commit. Explicit `inputFormat` survives file import, and URL
startup imports complete before the one-time `onInit` callback.

Readable patch fragments live in `../sdk/core-fragment.js`,
`../sdk/editor-fragment.js`, `../sdk/macro-controller.mjs` and
`../sdk/indigo-transport.mjs`. Their exact transformed content is verified in both
module formats during normal installation. A fragment edit must be propagated to
the corresponding versioned patch and reviewed manifest; the verifier rejects a
partial update. Use two context lines for the standalone patch so the unchanged
Base64 Worker source does not become a giant diff. Preserve upstream line endings
and validate the result with a fresh normal `npm ci`.

The standalone service can adopt an already-running transport, including its
listeners, queue and original Info promise. The SDK build extracts the actual
Worker Blob bytes without changing its WASM or protocol. Production React and
ReactDOM are owned by that SDK; the development host mounts through a DOM boundary.
The runtime build checks the bootstrap/micro/Macro dependency graph and emits an
audited asset manifest. Vendor patches are still applied for the iframe fallback
build, but that build neither generates nor outputs the native runtime assets.

Macro resource attempts have a 12-second deadline inside the controller's
15-second initialization deadline. Failed or hung requests receive fresh URLs;
successfully loaded code or CSS is reused. Retired/late attempts cannot replace
the current resource records or write into a new editor. Service teardown also
clears controller references retained through SDK memoization caches.

Run `npm run test:ketcher-runtime`, `npm run test:ketcher-deferred` against an
isolated development server, and the existing full `test:structure-engines` gate
after a capability or lifecycle change. `scripts/verify-ketcher-loading.mjs`
performs the paired application benchmark; it must run separately from builds
and other browser tests. Markdown maintenance and test-only edits do not rebuild
or reload the SDK.

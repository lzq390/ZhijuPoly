# Ketcher 3.8.0 / NexPoly r3

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

# Handoff runtime and synthetic assets

These files are packaging inputs. Apply them to the isolated frontend snapshot only.

## Integration

- Copy `overlay/build/handoff.ts` to `frontend/build/handoff.ts`; import `handoffPlugin` from `./build/handoff` in the staged Vite config and add `handoffPlugin()` only in `mode === "handoff"`.
- Set both Vite `server.proxy` and `preview.proxy` to `undefined` in handoff mode. The plugin deliberately does not forward any API.
- Overlay `assets/public/` on `frontend/public/`.
- Replace `frontend/src/constants/highThroughputDoePiRows.ts` with `assets/source/highThroughputDoePiRows.ts`. Keep `highThroughputDemoScenario.ts` unchanged: the four public DOE CSVs were regenerated from its current logic using the new synthetic rows.
- Replace the Google Fonts import in `frontend/src/index.css` with `@import url("/fonts/fonts.css");`; remove the Google Fonts import in `frontend/src/styles/polymer-desktop.css`. This single local stylesheet includes all four families.
- Delete `frontend/public/images/pdf-similarity/similar_paper_{1..6}.png` from the staged copy; update the six corresponding `PdfSimilarityDemoPanel.tsx` paths to `.svg`.
- `public/handoff/agent.html` is an optional local OpenScience embedded placeholder. It contains no agent implementation. If the app requires absolute URLs, derive its URL from `window.location.origin`, so the same source works on any dev/preview port.

## Runtime behavior

The plugin exports `handoffPlugin({ httpFixtures?: boolean } = {}): Plugin` and `handoffContentSecurityPolicy: string`. Its development and preview middleware serve the same synthetic HTTP downloads, including public CSV/XLSX templates, batch ZIP/XLSX/error files, DFT scientific result JSON and DFT ZIP. This is required because anchor downloads bypass the browser fetch mock.

Public assets are available at `/handoff/downloads/`. DFT uses `dft-bundle.zip`; batch uses `results.zip`, `results.xlsx`, and `input_errors.csv`; MD uses `result.csv`. `scientific_result.json` follows the synthetic Mock DFT response, and is distinct from `result.json`, a minimal trajectory example. Every ZIP and XLSX is a valid binary archive.

`/health` reports handoff mode. Unknown `/api` requests and unsupported methods return JSON with HTTP 501 and `HANDOFF_API_NOT_IMPLEMENTED`. Paths are mapped to fixed filenames; caller input cannot select arbitrary files. Source `public/` remains present after build, so preview serves identical downloads.

The CSP allows same-origin assets, data/blob renderers, WASM compilation, local workers and loopback Vite WebSocket connections. External frames, API connections, scripts, fonts and images are blocked. Inline scripts/styles remain allowed for Vite React refresh, Ketcher and existing component styles. `unsafe-eval` is required by the current native Ketcher SDK's string-compiled JavaScript helpers; a real-browser run confirmed that WASM permission alone leaves the editor unavailable. This permission applies only to the local handoff mode and does not permit external script sources. The policy is applied both as an HTTP header and an HTML meta element; `frame-ancestors` is header-only.

For existing Playwright layout/focus/motion scripts which intercept HTTP via `route.fulfill`, use the separate `handoff-fixtures` mode and `handoffPlugin({ httpFixtures: true })`, while skipping browser Mock installation in that mode. Its local HTTP fallback calls the same `createHandoffMockFetch` factory, parses JSON and multipart uploads, maintains isolated fixture state per HttpOnly demo cookie, and aborts streams when the client disconnects. Static download handling and unknown API 501 behavior are retained. `handoff-fixtures.mjs` and its `.d.mts` declaration keep the composite Node config type project separate from application types; Vite bundles its TypeScript implementation normally. The ordinary `handoff` mode continues to exercise the actual browser Mock and never enables HTTP business fixtures.

## Data replacement and provenance

- DOE source rows: 48 generated examples, 12 per property; synthetic IDs 900001–900048. The existing source model types and number of rows are preserved. No tests found that assert the original database row IDs. Numerical values and structures are synthetic and must not be described as measured or predicted results.
- `public/demo-data/doe_prior_*.csv`: generated from the unchanged scenario with the replacement source rows, so displayed IDs and downloadable rows agree.
- `public/samples/monomer-polymerization-batch/`: existing filenames retained for compatibility; both 120-row CSVs, metadata and README replaced. Structures are deliberately repeated for bulk-layout testing. No original database path, fingerprint or source rows remain.
- Paper previews: six new SVG placeholders with no source article content. Existing paper metadata already uses demonstration labels; it must continue to be presented as a demo.
- Fonts: original normal-style variable Exo, IBM Plex Sans, Roboto Mono and Outfit from the [official Google Fonts repository](https://github.com/google/fonts), pinned to commit `e44c4b011a820c2cbe2fd2cfa8052037d7edb571`. `public/fonts/sources.json` records exact original URLs, byte sizes and SHA-256 hashes; per-family OFL licenses are included. CSS declares only the UI's required weight ranges (400–700, Outfit 300–700); originals are unmodified and include their complete variable weight axes. No italic font files are bundled.

To regenerate local synthetic assets, run `python3 tools/frontend-handoff/assets/generate_synthetic_assets.py`, then `node tools/frontend-handoff/assets/generate_doe_csv.cjs` from the repository. The latter needs existing frontend TypeScript dependencies; packaging itself only copies the checked-in generated files.

## Verification performed

The plugin passed strict TypeScript checking against the current Vite types. Development and preview HTTP middleware were both exercised for every template format/role, batch files, DFT JSON and ZIP, unknown APIs, unsupported methods, HEAD requests, health and CSP headers. The optional HTTP-fixtures transport was exercised with a real Node 22 HTTP server for login/logout, per-cookie isolation, guest and empty scenarios, JSON bodies, multipart filenames, cancellable SSE, local DFT ZIP and unknown 501. Generated ZIP integrity, XLSX XML structure and every font provenance hash passed. Full app, Ketcher/WASM and browser layout checks are performed separately against the extracted candidate ZIP by the packager.

In the isolated debug extraction, the original browser scripts also passed: 19 geometry/state checks across five modules at 1440px, all eight drawer-focus combinations at 390/1440px with normal/reduced motion and empty/populated results, high-throughput S1 upload states at 1920px, and the 1440px motion suite including real native Ketcher retention across navigation. A first motion run overlapped a deliberate Vite config restart while files were being synchronized; the rerun against a stable copy passed unchanged. Formal archive acceptance must still use the final extracted candidate.

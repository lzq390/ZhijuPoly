import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { ASSET_NAMES, ASSET_PREFIX, BASE_INDEX_SHA256, MODULE_ANCHOR, createOverlay, writeOverlay } from './ui-patch.mjs';

// Read-only captured index from the exact local base image. No upstream bundle
// or production overlay fixture is needed for this independent build contract.
const BASE_INDEX = `<!doctype html>
<html lang="en" style="background-color: var(--background-base)">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>OpenScience</title>
    <link rel="icon" type="image/png" href="/favicon-96x96-v4.png" sizes="96x96" />
    <link rel="icon" type="image/svg+xml" href="/atlas-favicon.svg" />
    <link rel="shortcut icon" href="/favicon-v4.ico" />
    <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon-v4.png" />
    <link rel="manifest" href="/site.webmanifest" />
    <meta name="theme-color" content="#edf3f9" />
    <meta name="color-scheme" content="light" />
    <meta property="og:image" content="/social-share.png" />
    <meta property="twitter:image" content="/social-share.png" />
    <!-- Font families match ZhijuPoly; assets are served by this iframe. -->
    <link rel="preload" href="/fonts/zhijupoly/ibm-plex-sans-variable.ttf" as="font" type="font/ttf" crossorigin="anonymous" />
    <script id="openscience-theme-preload-script" src="/openscience-theme-preload.js"></script>
    <script type="importmap">{"imports":{"/assets/index-CtiA6yRV.js":"/assets/index-nexpoly-6ec6a90395a8.js"}}</script>
    <script type="module" crossorigin src="/assets/index-nexpoly-6ec6a90395a8.js"></script>
    <link rel="stylesheet" crossorigin href="/assets/index-DlN1gRLG.css">
  </head>
  <body class="antialiased overscroll-none text-12-regular overflow-hidden">
    <noscript>You need to enable JavaScript to run this app.</noscript>
    <div id="root" class="flex flex-col h-dvh p-px"></div>
  </body>
</html>
`;

const sourceAssets = () => ({
  'results-controller.mjs': Buffer.from("import { mount } from './results-view.mjs';\nmount();\n"),
  'results-view.mjs': Buffer.from('export function mount() {}\n'),
  'results-view.css': Buffer.from('.nexpoly-results { color: inherit; }\n'),
});

test('accepts only the inspected index and prepends the two fixed entries without rewriting existing markup', () => {
  assert.equal(createHash('sha256').update(BASE_INDEX).digest('hex'), BASE_INDEX_SHA256);
  const assets = sourceAssets();
  const overlay = createOverlay(Buffer.from(BASE_INDEX), assets);
  assert.match(overlay.assetHash, /^[a-f0-9]{64}$/);
  const prefix = `${ASSET_PREFIX}${overlay.assetHash}`;
  const addition = `    <!-- nexpoly-results-overlay -->\n`
    + `    <link rel="stylesheet" href="${prefix}/results-view.css">\n`
    + `    <script type="module" src="${prefix}/results-controller.js"></script>\n`;
  assert.equal(overlay.index.toString(), BASE_INDEX.replace(MODULE_ANCHOR, addition + MODULE_ANCHOR));
  assert.ok(overlay.index.toString().indexOf('/results-controller.js') < overlay.index.toString().indexOf(MODULE_ANCHOR));
  assert.deepEqual(Object.keys(overlay.assets).sort(), ['results-controller.js', 'results-view.css', 'results-view.js']);
  assert.equal(overlay.assets['results-controller.js'].toString(), "import { mount } from './results-view.js';\nmount();\n");
  assert.deepEqual(overlay.assets['results-view.js'], assets['results-view.mjs']);
  assert.deepEqual(overlay.assets['results-view.css'], assets['results-view.css']);
  assert.equal(assets['results-controller.mjs'].toString(), "import { mount } from './results-view.mjs';\nmount();\n");
});

test('all source content determines the immutable asset directory reproducibly', () => {
  const baseline = createOverlay(BASE_INDEX, sourceAssets());
  assert.deepEqual(createOverlay(BASE_INDEX, sourceAssets()), baseline);
  for (const name of ASSET_NAMES) {
    const assets = sourceAssets();
    assets[name] = Buffer.concat([assets[name], Buffer.from('\n')]);
    const changed = createOverlay(BASE_INDEX, assets);
    assert.notEqual(changed.assetHash, baseline.assetHash, name);
    assert.ok(changed.index.includes(Buffer.from(`${ASSET_PREFIX}${changed.assetHash}/results-controller.js`)));
  }
});

test('fails closed on modified base, missing or duplicate anchor, and a second application', () => {
  assert.throws(() => createOverlay(BASE_INDEX.replace('<title>OpenScience</title>', '<title>Changed</title>'), sourceAssets()), /SHA256/);
  assert.throws(() => createOverlay(BASE_INDEX.replace(MODULE_ANCHOR, ''), sourceAssets()), /exactly one/);
  assert.throws(() => createOverlay(BASE_INDEX.replace(MODULE_ANCHOR, MODULE_ANCHOR + MODULE_ANCHOR), sourceAssets()), /exactly one/);
  assert.throws(() => createOverlay(BASE_INDEX.replace('</head>', '<script type="module" src="/other.js"></script></head>'), sourceAssets()), /exactly one/);
  const first = createOverlay(BASE_INDEX, sourceAssets());
  assert.throws(() => createOverlay(first.index, sourceAssets()), /already present/);
});

test('rejects missing, extra, empty, or incompatible controller assets', () => {
  const missing = sourceAssets();
  delete missing['results-view.css'];
  assert.throws(() => createOverlay(BASE_INDEX, missing), /exactly the controller/);
  assert.throws(() => createOverlay(BASE_INDEX, { ...sourceAssets(), 'other.js': Buffer.from('x') }), /exactly the controller/);
  for (const name of ASSET_NAMES) {
    assert.throws(() => createOverlay(BASE_INDEX, { ...sourceAssets(), [name]: Buffer.alloc(0) }), /must not be empty/);
  }
  for (const text of ['no import', "import './results-view.mjs'; import './results-view.mjs';"]) {
    assert.throws(() => createOverlay(BASE_INDEX, { ...sourceAssets(), 'results-controller.mjs': text }), /import the results view exactly once/);
  }
  const doubleQuotes = { ...sourceAssets(), 'results-controller.mjs': 'import "./results-view.mjs";\n' };
  assert.equal(createOverlay(BASE_INDEX, doubleQuotes).assets['results-controller.js'].toString(), 'import "./results-view.js";\n');
});

test('writes only a new index and three readable assets and refuses to overwrite an existing tree', (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'nexpoly-ui-overlay-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const index = join(directory, 'index.html');
  const sources = join(directory, 'sources');
  const output = join(directory, 'output');
  writeFileSync(index, BASE_INDEX);
  mkdirSync(sources);
  for (const [name, bytes] of Object.entries(sourceAssets())) writeFileSync(join(sources, name), bytes);
  const hash = writeOverlay(index, sources, output);
  assert.equal(hash, createOverlay(BASE_INDEX, sourceAssets()).assetHash);
  assert.deepEqual(readdirSync(output).sort(), ['assets', 'index.html']);
  assert.deepEqual(readdirSync(join(output, 'assets', 'nexpoly-results', hash)).sort(), ['results-controller.js', 'results-view.css', 'results-view.js']);
  assert.deepEqual(readFileSync(join(output, 'index.html')), createOverlay(BASE_INDEX, sourceAssets()).index);
  assert.equal(readFileSync(index, 'utf8'), BASE_INDEX);
  assert.throws(() => writeOverlay(index, sources, output), /must not already exist/);
  rmSync(join(sources, 'results-view.css'));
  symlinkSync(index, join(sources, 'results-view.css'));
  assert.throws(() => writeOverlay(index, sources, join(directory, 'another-output')), /ordinary file/);
});

import { createHash } from 'node:crypto';
import { existsSync, lstatSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';

// This is the deployed 4522be0-production-dual index, inspected read-only from
// openscience-ui-20260922. This contract is separate from the production overlay.
export const BASE_INDEX_SHA256 = '5647ece9a54520ad8df4b04b2424293a54f3537f21293649729522144ddd3157';
export const MODULE_ANCHOR = '    <script type="module" crossorigin src="/assets/index-nexpoly-6ec6a90395a8.js"></script>';
export const ASSET_NAMES = Object.freeze(['results-controller.mjs', 'results-view.mjs', 'results-view.css']);
export const ASSET_PREFIX = '/assets/nexpoly-results/';
const OUTPUT_NAMES = { 'results-controller.mjs': 'results-controller.js', 'results-view.mjs': 'results-view.js', 'results-view.css': 'results-view.css' };

const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');

export function createOverlay(indexBytes, sourceAssets) {
  const index = Buffer.from(indexBytes).toString('utf8');
  if (index.includes(ASSET_PREFIX) || index.includes('nexpoly-results-overlay')) {
    throw new Error('Development results overlay is already present; refusing duplicate injection.');
  }
  if (index.split(MODULE_ANCHOR).length !== 2
      || [...index.matchAll(/<script\b[^>]*\btype=["']module["'][^>]*>/g)].length !== 1) {
    throw new Error('Expected exactly one approved application module injection anchor.');
  }
  if (digest(indexBytes) !== BASE_INDEX_SHA256) {
    throw new Error('Base UI index SHA256 differs from the inspected immutable image.');
  }
  if (Object.keys(sourceAssets).sort().join('\0') !== [...ASSET_NAMES].sort().join('\0')) {
    throw new Error('Results overlay must contain exactly the controller, view module and stylesheet.');
  }
  const hash = createHash('sha256');
  const assets = {};
  for (const name of ASSET_NAMES) {
    const bytes = Buffer.from(sourceAssets[name]);
    if (!bytes.length) throw new Error('Results overlay assets must not be empty.');
    // Length-prefix each entry so the common directory hash has no ambiguous
    // filename/content boundaries. Relative module imports keep working.
    hash.update(`${name}\0${bytes.length}\0`).update(bytes);
    assets[OUTPUT_NAMES[name]] = bytes;
  }
  // The inspected nginx mime.types recognizes .js, not .mjs. Only the newly
  // authored controller's relative import is rewritten; upstream code is never
  // read or changed. Both source modules remain directly testable as ESM.
  const controller = assets['results-controller.js'].toString('utf8');
  const viewImport = /(['"])\.\/results-view\.mjs\1/g;
  if ([...controller.matchAll(viewImport)].length !== 1) {
    throw new Error('Controller must import the results view exactly once by its relative module path.');
  }
  assets['results-controller.js'] = Buffer.from(controller.replace(viewImport, (match, quote) => `${quote}./results-view.js${quote}`));
  const assetHash = hash.digest('hex');
  const assetPath = `${ASSET_PREFIX}${assetHash}`;
  const addition = `    <!-- nexpoly-results-overlay -->\n`
    + `    <link rel="stylesheet" href="${assetPath}/results-view.css">\n`
    + `    <script type="module" src="${assetPath}/results-controller.js"></script>\n`;
  // Module scripts execute in document order. Install the controller's observer
  // before the existing app can issue its initial session request.
  return { index: Buffer.from(index.replace(MODULE_ANCHOR, addition + MODULE_ANCHOR)), assets, assetHash };
}

export function writeOverlay(indexPath, assetsDirectory, outputDirectory) {
  // Build output is always a fresh directory. Never patch an existing static
  // tree in place, and never follow source or destination symbolic links.
  if (existsSync(outputDirectory)) throw new Error('Overlay output directory must not already exist.');
  const ordinaryFile = (path) => {
    if (!lstatSync(path).isFile()) throw new Error('Overlay source must be an ordinary file.');
    return readFileSync(path);
  };
  if (!lstatSync(assetsDirectory).isDirectory()) throw new Error('Assets source must be an ordinary directory.');
  const sourceAssets = Object.fromEntries(ASSET_NAMES.map((name) => [name, ordinaryFile(join(assetsDirectory, name))]));
  const overlay = createOverlay(ordinaryFile(indexPath), sourceAssets);
  const directory = join(outputDirectory, 'assets', 'nexpoly-results', overlay.assetHash);
  mkdirSync(directory, { recursive: true, mode: 0o755 });
  writeFileSync(join(outputDirectory, 'index.html'), overlay.index, { mode: 0o644, flag: 'wx' });
  for (const [name, bytes] of Object.entries(overlay.assets)) writeFileSync(join(directory, name), bytes, { mode: 0o644, flag: 'wx' });
  return overlay.assetHash;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.length !== 5) throw new Error('Usage: ui-patch.mjs INDEX ASSETS_DIRECTORY OUTPUT_DIRECTORY');
  const assetHash = writeOverlay(...process.argv.slice(2));
  process.stdout.write(`Development UI overlay assets SHA256: ${assetHash}\n`);
}

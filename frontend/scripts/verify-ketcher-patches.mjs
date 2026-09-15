import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
const root = new URL('../', import.meta.url);
const read = path => readFile(new URL(path, root));
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const manifest = JSON.parse(await read('patches/ketcher-manifest.json'));
for (const [name, version] of Object.entries(manifest.versions)) {
  const actual = JSON.parse(await read(`node_modules/${name}/package.json`)).version;
  if (actual !== version) throw new Error(`${name}: expected ${version}, installed ${actual}`);
}
for (const [path, expected] of [...Object.entries(manifest.patches), ...Object.entries(manifest.fragments || {}), ...Object.entries(manifest.files).map(([path, entry]) => [path, entry.patched])]) {
  if (sha(await read(path)) !== expected) throw new Error(`Ketcher patch verification failed: ${path}. Run normal npm ci; do not update the manifest to bypass verification.`);
}
for (const [fragment, modules, transform] of [
  ['sdk/core-fragment.js', ['ketcher-core/dist/index.modern.js', 'ketcher-core/dist/index.js'], value => value],
  ['sdk/editor-fragment.js', ['ketcher-react/dist/index.js', 'ketcher-react/dist/cjs/index.js'], value => value],
  ['sdk/macro-controller.mjs', ['ketcher-react/dist/index.js', 'ketcher-react/dist/cjs/index.js'], value => value.replace('export function containsMacro', 'function containsMacro').replace('export class MacroController', 'class MacroController')],
  ['sdk/indigo-transport.mjs', ['ketcher-standalone/dist/main.js', 'ketcher-standalone/dist/cjs/main.js'], value => value.replace('export class IndigoTransport', 'class NexPolyIndigoTransport')]
]) {
  const source = transform(String(await read(fragment)));
  for (const module of modules) if (!String(await read('node_modules/' + module)).includes(source)) {
    throw new Error(`Audited fragment ${fragment} differs from installed ${module}. Regenerate the versioned patch and reinstall.`);
  }
}
console.log(`Verified ${manifest.revision}: ${Object.keys(manifest.files).length} SDK artifacts and ${Object.keys(manifest.patches).length} versioned patches.`);

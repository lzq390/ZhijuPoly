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
for (const [path, expected] of [...Object.entries(manifest.patches), ...Object.entries(manifest.files).map(([path, entry]) => [path, entry.patched])]) {
  if (sha(await read(path)) !== expected) throw new Error(`Ketcher patch verification failed: ${path}. Run normal npm ci; do not update the manifest to bypass verification.`);
}
console.log(`Verified ${manifest.revision}: ${Object.keys(manifest.files).length} SDK artifacts and ${Object.keys(manifest.patches).length} versioned patches.`);

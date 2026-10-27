import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

const root = new URL('../', import.meta.url);
const sha = bytes => createHash('sha256').update(bytes).digest('hex');

// Unlike the npm SDK, the legacy iframe is a committed upstream distribution.
// Its reviewed recipe is reversible and accepts only the exact pinned bytes.
export async function verifyIframePatch({ write = false } = {}) {
  const recipe = JSON.parse(await readFile(new URL('patches/ketcher-iframe-paste.json', root)));
  const file = new URL(recipe.file, root);
  let bytes = await readFile(file);
  function replaceOnce(source, before, after) {
    const first = source.indexOf(before);
    if (first < 0 || source.indexOf(before, first + before.length) >= 0) {
      throw new Error('Iframe patch anchor must occur exactly once');
    }
    return source.slice(0, first) + after + source.slice(first + before.length);
  }
  if (write && sha(bytes) === recipe.upstream) {
    let source = bytes.toString('utf8');
    for (const patch of recipe.replacements) source = replaceOnce(source, patch.before, patch.after);
    bytes = Buffer.from(source);
    if (sha(bytes) !== recipe.patched) throw new Error('Iframe patch result differs from reviewed digest');
    await writeFile(file, bytes);
  }
  if (sha(bytes) !== recipe.patched) throw new Error('Iframe bundle differs from reviewed patched digest');
  let upstream = bytes.toString('utf8');
  for (const patch of [...recipe.replacements].reverse()) upstream = replaceOnce(upstream, patch.after, patch.before);
  if (sha(upstream) !== recipe.upstream) throw new Error('Iframe reverse patch differs from pinned upstream');
  return recipe;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (process.argv.slice(2).some(value => value !== '--write') || process.argv.length > 3) {
    throw new Error('Usage: node scripts/patch-ketcher-iframe.mjs [--write]');
  }
  const recipe = await verifyIframePatch({ write: process.argv.includes('--write') });
  console.log(`Verified reversible iframe ${recipe.sdkVersion} paste lifecycle patch.`);
}

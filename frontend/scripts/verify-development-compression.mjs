// Exercise the actual Vite response pipeline, including 304/outdated deps.
import assert from 'node:assert/strict';
import { request } from 'node:http';
import { gunzipSync, brotliDecompressSync } from 'node:zlib';

const base = process.env.STRUCTURE_BASE_URL || 'http://127.0.0.1:5912';
assert.equal(new URL(base).hostname, '127.0.0.1');
async function get(path, headers = {}, method = 'GET') {
  return new Promise((resolve, reject) => {
    const req = request(new URL(path, base), { method, headers: { Origin: base, ...headers } }, response => {
      const chunks = [];
      response.on('data', data => chunks.push(data));
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, body: Buffer.concat(chunks) }));
      response.on('error', reject);
    });
    req.on('error', reject); req.end();
  });
}
const runtime = await get('/src/components/structure-workbench/KetcherReactRuntime.tsx');
assert.equal(runtime.status, 200);
const dependency = runtime.body.toString().match(/['"]([^'"]*\/node_modules\/\.vite\/deps\/ketcher-standalone\.js[^'"]*)['"]/)?.[1];
assert.ok(dependency, 'Vite must resolve the real optimized dependency');
const original = await get(dependency, { 'Accept-Encoding': 'identity' });
assert.equal(original.status, 200);
assert.equal(original.headers['content-encoding'], undefined);
assert.ok(original.body.length > 1e6);
assert.match(original.headers['cache-control'], /immutable/);
for (const encoding of ['gzip', 'br']) {
  const compressed = await get(dependency, { 'Accept-Encoding': encoding });
  assert.equal(compressed.status, 200);
  assert.equal(compressed.headers['content-encoding'], encoding);
  assert.equal(compressed.headers['content-length'], undefined);
  assert.equal(compressed.headers.etag, original.headers.etag);
  assert.equal(compressed.headers['cache-control'], original.headers['cache-control']);
  assert.match(compressed.headers.vary, /Origin/);
  assert.match(compressed.headers.vary, /Accept-Encoding/);
  assert.deepEqual((encoding === 'gzip' ? gunzipSync : brotliDecompressSync)(compressed.body), original.body);
  assert.ok(compressed.body.length < original.body.length / 2);
  const cached = await get(dependency, { 'Accept-Encoding': encoding, 'If-None-Match': original.headers.etag });
  assert.equal(cached.status, 304);
  assert.equal(cached.body.length, 0);
  assert.equal(cached.headers['content-encoding'], undefined);
  assert.match(cached.headers.vary, /Origin/);
  assert.match(cached.headers.vary, /Accept-Encoding/);
  console.log(JSON.stringify({ encoding, originalBytes: original.body.length, compressedBytes: compressed.body.length, cached: cached.status }));
}
const head = await get(dependency, { 'Accept-Encoding': 'gzip' }, 'HEAD');
assert.equal(head.body.length, 0);
assert.equal(head.headers['content-encoding'], undefined);
assert.match(head.headers.vary, /Accept-Encoding/);
const range = await get(dependency, { 'Accept-Encoding': 'gzip', Range: 'bytes=0-99' });
assert.equal(range.headers['content-encoding'], undefined);
const outdated = new URL(dependency, base);
outdated.searchParams.set('v', 'obsolete-version');
const stale = await get(outdated.pathname + outdated.search, { 'Accept-Encoding': 'gzip' });
assert.equal(stale.status, 504);
assert.equal(stale.headers['content-encoding'], undefined);
for (const path of ['/src/main.tsx', '/@vite/client', '/src/does-not-exist.tsx']) {
  const response = await get(path, { 'Accept-Encoding': 'gzip' });
  assert.equal(response.headers['content-encoding'], undefined, path);
}
console.log(JSON.stringify({ passed: true, checks: ['byte-equivalence', 'gzip', 'brotli', '304', 'vary', 'head', 'range', 'outdated-dep', 'source-and-hmr'] }));

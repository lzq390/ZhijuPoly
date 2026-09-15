import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer, request } from 'node:http';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runtimeMiddleware } from '../build/ketcher-runtime-http.mjs';

test('compressed SDK delivery preserves negotiation, conditional GET, HEAD, retries and previous versions', async () => {
  const root = await mkdtemp(join(tmpdir(), 'ketcher-http-'));
  const runtime = { directory: join(root, '0123456789abcdef0123'), manifest: { version: '0123456789abcdef0123', assets: [
    { file: 'worker.js', sha256: 'raw', bytes: 3 }, { file: 'worker.js.br', sha256: 'br', bytes: 2 }, { file: 'worker.js.gz', sha256: 'gz', bytes: 2 }
  ] } };
  await mkdir(runtime.directory); await writeFile(join(runtime.directory, 'manifest.json'), JSON.stringify(runtime.manifest));
  for (const [file, data] of [['worker.js', 'raw'], ['worker.js.br', 'br'], ['worker.js.gz', 'gz']]) await writeFile(join(runtime.directory, file), data);
  let current = runtime;
  const serve = runtimeMiddleware(() => current);
  const server = createServer((req, res) => { void serve(req, res, () => { res.statusCode = 404; res.end(); }).catch(error => { res.statusCode = 500; res.end(String(error)); }); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const get = (headers = {}, method = 'GET', suffix = 'worker.js') => new Promise((resolve, reject) => {
    request(`http://127.0.0.1:${server.address().port}/assets/ketcher/${runtime.manifest.version}/${suffix}`, { method, headers }, response => {
      const chunks = []; response.on('data', chunk => chunks.push(chunk));
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, body: Buffer.concat(chunks).toString() }));
    }).on('error', reject).end();
  });
  try {
    const br = await get({ 'accept-encoding': 'gzip, br' });
    assert.equal(br.body, 'br'); assert.equal(br.headers.vary, 'Accept-Encoding');
    assert.match(br.headers['content-type'], /javascript/); assert.equal(br.headers['content-length'], '2');
    assert.equal((await get({ 'accept-encoding': 'br;q=0,gzip;q=0.5' })).body, 'gz');
    assert.equal((await get({ 'accept-encoding': 'identity;q=1,gzip;q=0.5' })).body, 'raw');
    assert.equal((await get({ 'accept-encoding': '*;q=0' })).status, 406);
    assert.equal((await get()).body, 'raw');
    const head = await get({ 'accept-encoding': 'br' }, 'HEAD'); assert.equal(head.body, ''); assert.equal(head.headers['content-length'], '2');
    for (const etag of ['W/"br"', '*', '"other", "br"']) {
      const cached = await get({ 'accept-encoding': 'br', 'if-none-match': etag });
      assert.equal(cached.status, 304); assert.equal(cached.body, ''); assert.equal(cached.headers['content-length'], undefined);
    }
    assert.equal((await get({}, 'POST')).status, 405);
    assert.equal((await get({}, 'GET', 'retry/1/worker.js')).body, 'raw');
    current = { directory: join(root, 'abcdef0123456789abcd'), manifest: { version: 'abcdef0123456789abcd', assets: [] } };
    assert.equal((await get()).body, 'raw');
    assert.equal((await get({}, 'GET', 'missing.js')).status, 404);
  } finally { await new Promise(resolve => server.close(resolve)); await rm(root, { recursive: true, force: true }); }
});

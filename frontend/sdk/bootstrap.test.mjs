import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

let next = 0;
async function fixture() {
  const workers = [];
  globalThis.window = new EventTarget();
  globalThis.location = { pathname: '/structure-workbench' };
  globalThis.document = { createElement: () => ({ dataset: {}, remove() {} }),
    head: { append: link => queueMicrotask(() => link.onload?.()) } };
  globalThis.Worker = class {
    constructor() { this.sent = []; this.terminated = 0; workers.push(this); }
    postMessage(value) { this.sent.push(value); }
    terminate() { ++this.terminated; }
    reply() { this.onmessage({ data: { type: 0, hasError: false, payload: '3.8-test' } }); }
  };
  const source = (await readFile(new URL('./bootstrap.mjs', import.meta.url), 'utf8'))
    .replace("'./indigo-transport.mjs'", JSON.stringify(new URL('./indigo-transport.mjs', import.meta.url).href))
    .replace(/import \{[^\n]+\} from 'virtual:ketcher-assets';/, `const workerUrl='worker.js', macroUrl='https://localhost/macro.js', macroStyleUrl='https://localhost/macro.css', microUrl='data:text/javascript,export const mounted=false';`)
    .replace("new URL('./ketcher.css', import.meta.url).href", "'https://localhost/ketcher.css'");
  const bootstrap = await import('data:text/javascript;base64,' + Buffer.from(source + '\n// ' + ++next).toString('base64'));
  return { bootstrap, workers, session: { check() {} } };
}
async function macroFixture() {
  const imports = [], styles = [];
  globalThis.window = new EventTarget();
  globalThis.location = { pathname: '/structure-workbench' };
  globalThis.document = {
    createElement: () => ({ dataset: {}, remove() { this.removed = true; } }),
    head: { append: link => styles.push(link) }
  };
  globalThis.__macroImport = url => new Promise((resolve, reject) => imports.push({ url, resolve, reject }));
  const source = (await readFile(new URL('./bootstrap.mjs', import.meta.url), 'utf8'))
    .replace("'./indigo-transport.mjs'", JSON.stringify(new URL('./indigo-transport.mjs', import.meta.url).href))
    .replace(/import \{[^\n]+\} from 'virtual:ketcher-assets';/, `const workerUrl='worker.js', macroUrl='https://localhost/macro.js', macroStyleUrl='https://localhost/macro.css', microUrl='data:text/javascript,export const mounted=false';`)
    .replace('import(/* @vite-ignore */ url.href)', 'globalThis.__macroImport(url.href)');
  const bootstrap = await import('data:text/javascript;base64,' + Buffer.from(source + '\n// macro ' + ++next).toString('base64'));
  return { bootstrap, imports, styles };
}
test('a visible service adopts one Worker and the original Info promise exactly once', async () => {
  const { bootstrap, workers, session } = await fixture();
  bootstrap.prepareInitial(); bootstrap.prepareInitial();
  assert.equal(workers.length, 1); assert.deepEqual(workers[0].sent, [{ type: 0 }]);
  const transport = bootstrap.takeTransport(session);
  const first = transport.info(); assert.equal(transport.info(), first);
  workers[0].reply(); assert.equal((await first).indigoVersion, '3.8-test');
  bootstrap.prepareInitial(); bootstrap.cancelPrepared();
  assert.equal(workers.length, 1); assert.equal(workers[0].terminated, 0);
  assert.throws(() => bootstrap.takeTransport(session), /already owns/);
  transport.destroy(); assert.equal(workers[0].terminated, 1);
});
test('cancelled navigation retires the unclaimed lease; a later session receives a fresh Worker', async () => {
  const { bootstrap, workers, session } = await fixture();
  bootstrap.prepareInitial();
  window.dispatchEvent(new Event('nexpoly:structure-navigation'));
  assert.equal(workers[0].terminated, 1);
  const transport = bootstrap.takeTransport(session);
  assert.equal(workers.length, 2); assert.equal(transport.worker, workers[1]);
  transport.destroy();
});
test('an aborted session cannot claim the preheated transport', async () => {
  const { bootstrap, workers } = await fixture();
  bootstrap.prepareInitial();
  assert.throws(() => bootstrap.takeTransport({ check() { throw new DOMException('retired', 'AbortError'); } }), { name: 'AbortError' });
  bootstrap.cancelPrepared(); assert.equal(workers[0].terminated, 1);
});
test('the initial lease expires after Info completes if nobody claims it', async t => {
  const { bootstrap, workers } = await fixture();
  t.mock.timers.enable({ apis: ['setTimeout'] });
  try {
    bootstrap.prepareInitial(); workers[0].reply();
    await Promise.resolve();
    t.mock.timers.tick(15001);
    assert.equal(workers[0].terminated, 1);
  } finally { t.mock.timers.reset(); bootstrap.cancelPrepared(); }
});
test('a failed Macro stylesheet retries with a new URL and reuses successfully evaluated code', async () => {
  const { bootstrap, imports, styles } = await macroFixture();
  const first = bootstrap.loadMacro();
  const expected = { default: 'macro-component' };
  imports[0].resolve(expected);
  await Promise.resolve(); await Promise.resolve();
  styles[0].onerror();
  await assert.rejects(first, /stylesheet failed/);
  assert.equal(styles[0].removed, true);
  const second = bootstrap.loadMacro();
  assert.equal(imports.length, 1);
  assert.match(styles[1].href, /\?attempt=1$/);
  styles[1].onload();
  assert.equal(await second, expected);
  assert.equal(bootstrap.loadMacro(), second);
});
test('a hung Macro import gets a fresh request; its late rejection cannot poison the successful retry', async t => {
  const { bootstrap, imports, styles } = await macroFixture();
  t.mock.timers.enable({ apis: ['setTimeout'] });
  try {
    const first = bootstrap.loadMacro();
    styles[0].onload();
    await Promise.resolve(); await Promise.resolve();
    const rejected = assert.rejects(first, /resources timed out/);
    t.mock.timers.tick(12001);
    await rejected;
    const second = bootstrap.loadMacro();
    assert.equal(styles.length, 1, 'The successful CSS should be reused');
    assert.match(imports[1].url, /\?attempt=1$/);
    imports[0].reject(new Error('late network failure'));
    const expected = { default: 'retry-component' };
    imports[1].resolve(expected);
    assert.equal(await second, expected);
    assert.equal(bootstrap.loadMacro(), second);
    assert.equal(imports.length, 2);
  } finally { t.mock.timers.reset(); }
});
test('a timed-out Macro stylesheet is removed and late events cannot finish the next attempt', async t => {
  const { bootstrap, imports, styles } = await macroFixture();
  t.mock.timers.enable({ apis: ['setTimeout'] });
  try {
    const first = bootstrap.loadMacro();
    const lateLoad = styles[0].onload;
    const rejected = assert.rejects(first, /resources timed out/);
    t.mock.timers.tick(12001);
    await rejected;
    assert.equal(styles[0].removed, true);
    assert.equal(styles[0].onload, null);
    const second = bootstrap.loadMacro();
    assert.match(styles[1].href, /\?attempt=1$/);
    assert.match(imports[1].url, /\?attempt=1$/);
    lateLoad(); imports[0].resolve({ default: 'retired-attempt' });
    let completed = false;
    second.then(() => { completed = true; });
    await Promise.resolve(); await Promise.resolve();
    assert.equal(completed, false);
    const expected = { default: 'current-attempt' };
    imports[1].resolve(expected); styles[1].onload();
    assert.equal(await second, expected);
  } finally { t.mock.timers.reset(); }
});

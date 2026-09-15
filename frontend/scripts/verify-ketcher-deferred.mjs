import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const base = process.env.KETCHER_DEFERRED_URL || 'http://127.0.0.1:5962';
const output = resolve(process.env.KETCHER_DEFERRED_OUTPUT || '/tmp/nexpoly-ketcher-deferred');
assert.equal(new URL(base).hostname, '127.0.0.1');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const cases = [];
async function check(name, run) {
  if (process.env.KETCHER_DEFERRED_CASES && !process.env.KETCHER_DEFERRED_CASES.split(',').includes(name)) return;
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.setDefaultTimeout(15000);
  const errors = [], requests = [];
  page.on('pageerror', error => errors.push(String(error)));
  page.on('request', request => requests.push(request.url()));
  await page.addInitScript(() => {
    window.__workers = { created: 0, active: 0, info: 0 };
    window.Worker = new Proxy(window.Worker, { construct(Type, args) {
      const worker = new Type(...args), record = window.__workers;
      ++record.created; ++record.active;
      const send = worker.postMessage.bind(worker), stop = worker.terminate.bind(worker);
      let retired = false;
      worker.postMessage = (message, ...rest) => { if (message?.type === 0) ++record.info; return send(message, ...rest); };
      worker.terminate = () => { if (!retired) { retired = true; --record.active; } stop(); };
      return worker;
    } });
  });
  const result = { name, passed: false, errors, requests };
  const ready = async () => {
    await page.goto(base + '/structure-workbench', { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => document.querySelector('[data-editor-status="ready"]'));
  };
  try { result.data = await run(page, ready, requests); assert.deepEqual(errors, []); result.passed = true; }
  catch (error) { result.error = String(error.stack); await page.screenshot({ path: resolve(output, name + '.png') }).catch(() => {}); }
  finally { cases.push(result); console.log(JSON.stringify({ name, passed: result.passed, error: result.error })); await page.close(); }
}

await check('micro-and-annotation-clear', async (page, ready, requests) => {
  await ready();
  assert.deepEqual(await page.evaluate(() => window.__workers), { created: 1, active: 1, info: 1 });
  assert.equal(await page.evaluate(() => ketcher.structService.nexpolyMacroController.state), 'idle');
  assert.ok(!requests.some(url => /\/macro-[^/]+\.js/.test(url)));
  await page.evaluate(() => ketcher.setMolecule('CCO'));
  await page.getByTestId('text').click();
  const area = await page.locator('[data-structure-editor] svg').first().boundingBox();
  await page.mouse.click(area.x + 180, area.y + 160);
  await page.getByTestId('text-editor').fill('Deferred SDK label');
  await page.getByRole('button', { name: 'Apply', exact: true }).click();
  await page.waitForFunction(async () => (await ketcher.getKet()).includes('Deferred SDK label'));
  await page.evaluate(() => ketcher.ensureMacroReady());
  assert.equal(await page.evaluate(() => Boolean(window.isPolymerEditorTurnedOn)), false);
  assert.equal(await page.evaluate(() => ketcher.getSmiles()), 'CCO');
  assert.ok(await page.evaluate(async () => (await ketcher.getKet()).includes('Deferred SDK label')));
  await page.evaluate(() => ketcher.clear());
  assert.equal(await page.evaluate(() => ketcher.getSmiles()), '');
  assert.ok(!await page.evaluate(async () => (await ketcher.getKet()).includes('Deferred SDK label')));
});
await check('direct-helm-and-concurrent-modes', async (page, ready) => {
  await ready();
  await page.evaluate(async () => {
    await ketcher.setHelm('PEPTIDE1{A.G.C}$$$$V2.0');
    await ketcher.switchToMoleculesMode();
  });
  assert.equal(await page.evaluate(() => ketcher.getSequence()), 'AGC');
  await page.evaluate(() => Promise.all([
    ketcher.switchToMacromoleculesMode(), ketcher.switchToMoleculesMode(), ketcher.switchToMacromoleculesMode()
  ]));
  assert.deepEqual(await page.evaluate(() => ({ view: window.isPolymerEditorTurnedOn,
    model: ketcher.structService.nexpolyMacroController.macro._type })), { view: true, model: 1 });
  assert.equal(await page.evaluate(() => ketcher.getSequence()), 'AGC');
  await page.evaluate(async () => { await ketcher.clear(); await ketcher.switchToMoleculesMode(); });
  assert.equal(await page.evaluate(() => ketcher.getSmiles()), '');
});
await check('macro-fragment-preserves-existing-molecule', async (page, ready) => {
  await ready();
  await page.evaluate(async () => {
    await ketcher.setMolecule('CCO');
    await ketcher.addFragment('PEPTIDE1{A.G.C}$$$$V2.0');
    await Promise.all([ketcher.switchToMacromoleculesMode(), ketcher.switchToMoleculesMode()]);
  });
  assert.equal(await page.evaluate(() => ketcher.getSmiles()), 'CCO.***');
  assert.equal(await page.evaluate(() => ketcher.editor.struct().atoms.size), 25);
});
for (const [name, source, format, expected] of [
  ['raw-helm', 'PEPTIDE1{A.G.C}$$$$V2.0', undefined, 'AGC'],
  ['explicit-fasta', '>Sequence1\nAGC', 'chemical/x-fasta', 'AGC'],
  ['explicit-peptide', 'AGC', 'chemical/x-peptide-sequence', 'AGC'],
  ['explicit-three-letter', 'AlaGlyCys', 'chemical/x-peptide-sequence-3-letter', 'AGC']
]) await check(name, async (page, ready) => {
  await ready();
  await page.evaluate(([source, format]) => ketcher.setMolecule(source, { inputFormat: format }), [source, format]);
  assert.equal(await page.evaluate(() => ketcher.getSequence()), expected);
  assert.equal(await page.evaluate(() => Boolean(window.isPolymerEditorTurnedOn)), false);
});
await check('file-dialog-direct-helm', async (page, ready) => {
  await ready();
  await page.getByTestId('open-file-button').click();
  await page.getByTestId('paste-from-clipboard-button').click();
  await page.getByTestId('open-structure-textarea').fill('PEPTIDE1{A.G.C}$$$$V2.0');
  await page.getByTestId('open-as-new-button').click();
  await page.waitForFunction(() => ketcher.editor.struct().atoms.size > 0);
  await page.evaluate(() => ketcher.switchToMoleculesMode());
  assert.equal(await page.evaluate(() => ketcher.getSequence()), 'AGC');
});
await check('formatter-and-library-gates', async (page, ready) => {
  await ready();
  const data = await page.evaluate(async () => {
    const factory = new ketcher.formatterFactory.constructor(ketcher.structService);
    const parsed = await factory.create('helm').getStructureFromStringAsync('PEPTIDE1{A.G.C}$$$$V2.0');
    ketcher.editor.struct(parsed);
    await ketcher.switchToMoleculesMode();
    const exported = await factory.create('fasta').getStructureFromStructAsync(ketcher.editor.struct());
    const macro = ketcher.structService.nexpolyMacroController.macro;
    const template = structuredClone(Object.values(macro.monomersLibraryParsedJson).find(item => item?.type === 'monomerTemplate'));
    template.id = 'NEXPOLY_TEST_MONOMER'; template.alias = 'NEXPOLY_TEST_MONOMER';
    const library = { root: { templates: [{ $ref: 'monomerTemplate-NEXPOLY_TEST_MONOMER' }] }, 'monomerTemplate-NEXPOLY_TEST_MONOMER': template };
    ketcher.updateMonomersLibrary(library);
    return { exported, customMonomer: !!macro.monomersLibraryParsedJson['monomerTemplate-NEXPOLY_TEST_MONOMER'],
      state: ketcher.structService.nexpolyMacroController.state, sequence: await ketcher.getSequence() };
  });
  assert.equal(data.state, 'ready'); assert.match(data.exported, /AGC/); assert.equal(data.sequence, 'AGC'); assert.ok(data.customMonomer);
  return data;
});
await check('macro-request-retry-preserves-micro', async (page, ready, requests) => {
  let fail = true;
  await page.route(/\/macro-[^/]+\.js/, route => fail ? route.abort('failed') : route.continue());
  await ready(); await page.evaluate(() => ketcher.setMolecule('CCO'));
  assert.match(await page.evaluate(() => ketcher.ensureMacroReady().then(() => 'unexpected', error => String(error))), /fetch|import|load/i);
  assert.equal(await page.evaluate(() => ketcher.getSmiles()), 'CCO');
  fail = false;
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await page.waitForFunction(() => window.isPolymerEditorTurnedOn === true);
  await page.evaluate(() => ketcher.switchToMoleculesMode());
  assert.equal(await page.evaluate(() => ketcher.getSmiles()), 'CCO');
  assert.ok(requests.some(url => /macro-.*\?attempt=1/.test(url)));
  assert.deepEqual(await page.evaluate(() => window.__workers), { created: 1, active: 1, info: 1 });
});
await check('macro-stylesheet-retry-preserves-micro', async (page, ready, requests) => {
  let fail = true;
  await page.route(/\/macro-[^/]+\.css/, route => fail ? route.abort('failed') : route.continue());
  await ready(); await page.evaluate(() => ketcher.setMolecule('CCO'));
  assert.match(await page.evaluate(() => ketcher.ensureMacroReady().then(() => 'unexpected', error => String(error))), /stylesheet failed/);
  assert.equal(await page.evaluate(() => ketcher.getSmiles()), 'CCO');
  fail = false;
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await page.waitForFunction(() => window.isPolymerEditorTurnedOn === true);
  await page.evaluate(() => ketcher.switchToMoleculesMode());
  assert.equal(await page.evaluate(() => ketcher.getSmiles()), 'CCO');
  assert.ok(requests.some(url => /macro-.*\.css\?attempt=1/.test(url)));
});
await check('macro-timeout-retry-ignores-late-request', async (page, ready, requests) => {
  let release;
  const paused = new Promise(resolve => { release = resolve; });
  let initialUrl;
  await page.route(/\/macro-[^/]+\.js/, async route => {
    if (!new URL(route.request().url()).searchParams.has('attempt')) {
      initialUrl = route.request().url(); await paused;
    }
    await route.continue().catch(() => {});
  });
  try {
    await ready(); await page.evaluate(() => ketcher.setMolecule('CCO'));
    assert.match(await page.evaluate(() => ketcher.ensureMacroReady().then(() => 'unexpected', error => String(error))), /resources timed out/);
    assert.equal(await page.evaluate(() => ketcher.getSmiles()), 'CCO');
    await page.getByRole('button', { name: '重试', exact: true }).click();
    await page.waitForFunction(() => window.isPolymerEditorTurnedOn === true);
    await page.evaluate(() => ketcher.switchToMoleculesMode());
    assert.ok(requests.some(url => /macro-.*\?attempt=1/.test(url)));
    const late = page.waitForResponse(response => response.url() === initialUrl);
    release(); await (await late).finished();
    assert.equal(await page.evaluate(() => ketcher.getSmiles()), 'CCO');
    assert.equal(await page.evaluate(() => ketcher.structService.nexpolyMacroController.state), 'ready');
    assert.deepEqual(await page.evaluate(() => window.__workers), { created: 1, active: 1, info: 1 });
  } finally { release(); }
});
await check('retirement-rejects-macro-waiters', async (page, ready) => {
  let release;
  const paused = new Promise(resolve => { release = resolve; });
  await page.route(/\/macro-[^/]+\.js/, async route => { await paused; await route.continue().catch(() => {}); });
  await ready();
  await page.evaluate(() => {
    window.__pendingMacro = Promise.allSettled([ketcher.ensureMacroReady(), ketcher.switchToMacromoleculesMode()]);
    ketcher.structService.destroy();
  });
  const settled = await page.evaluate(() => window.__pendingMacro.then(values => values.map(value => ({ status: value.status, error: value.reason?.name }))));
  assert.deepEqual(settled, [{ status: 'rejected', error: 'AbortError' }, { status: 'rejected', error: 'AbortError' }]);
  assert.equal(await page.evaluate(() => window.__workers.active), 0);
  release();
});
await check('ordinary-page-resource-isolation', async (page, _ready, requests) => {
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('[data-module-content]');
  assert.equal(await page.evaluate(() => window.__workers.created), 0);
  assert.ok(!requests.some(url => /\/assets\/ketcher\/|node_modules\/.+ketcher/.test(url)));
});
await check('leave-during-startup-and-history', async page => {
  let release;
  const paused = new Promise(resolve => { release = resolve; });
  await page.route(/\/assets\/ketcher\/.*\/micro-[^/]+\.js/, async route => {
    await paused; await route.continue().catch(() => {});
  });
  await page.goto(base + '/structure-workbench', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.__workers.active === 1 && document.querySelector('[data-module-content]'));
  await page.evaluate(() => { history.pushState({}, '', '/'); dispatchEvent(new PopStateEvent('popstate')); });
  await page.waitForFunction(() => window.__workers.active === 0);
  release();
  await page.waitForFunction(() => [...document.querySelectorAll('[data-editor-status]')].every(root => root.closest('[hidden],[aria-hidden="true"]')));
  await page.goBack({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelector('[data-editor-status="ready"]'));
  await page.evaluate(() => ketcher.setMolecule('CCO'));
  await page.goForward({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => [...document.querySelectorAll('[data-editor-status]')].every(root => root.closest('[hidden],[aria-hidden="true"]')));
  await page.goBack({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelector('[data-editor-status="ready"]'));
  assert.equal(await page.evaluate(() => ketcher.getSmiles()), 'CCO');
  assert.equal(page.workers().length, 1);
});
for (const [name, pattern] of [
  ['initial-javascript-retry', /\/assets\/ketcher\/.*\/shared-[^/]+\.js/],
  ['initial-stylesheet-retry', /\/assets\/ketcher\/.*\/ketcher\.css/]
]) await check(name, async (page, _ready, requests) => {
  let fail = true;
  await page.route(pattern, route => fail ? route.abort('failed') : route.continue());
  await page.goto(base + '/structure-workbench', { waitUntil: 'domcontentloaded' });
  await page.getByRole('button', { name: '重试加载画板', exact: true }).waitFor();
  fail = false;
  await page.getByRole('button', { name: '重试加载画板', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[data-editor-status="ready"]'));
  await page.evaluate(() => ketcher.setMolecule('CCO'));
  assert.equal(await page.evaluate(() => ketcher.getSmiles()), 'CCO');
  assert.equal(page.workers().length, 1);
  assert.ok(requests.some(url => /\/retry\/\d+\//.test(url)));
});
for (const [name, pattern] of [
  ['initial-javascript-timeout-retry', /\/assets\/ketcher\/.*\/shared-[^/]+\.js/],
  ['initial-stylesheet-timeout-retry', /\/assets\/ketcher\/.*\/ketcher\.css/],
  ['initial-bootstrap-timeout-retry', /\/assets\/ketcher\/.*\/bootstrap-[^/]+\.js/]
]) await check(name, async (page, _ready, requests) => {
  let release;
  const paused = new Promise(resolve => { release = resolve; });
  let original, settleOriginal;
  const originalDone = new Promise(resolve => { settleOriginal = resolve; });
  page.on('response', response => {
    if (response.url() === original) void response.finished().then(settleOriginal, settleOriginal);
  });
  page.on('requestfailed', request => { if (request.url() === original) settleOriginal(); });
  await page.route(pattern, async route => {
    if (!route.request().url().includes('/retry/')) {
      original = route.request().url(); await paused;
    }
    await route.continue().catch(() => {});
  });
  try {
    await page.goto(base + '/structure-workbench', { waitUntil: 'domcontentloaded' });
    await page.getByRole('button', { name: '重试加载画板', exact: true }).waitFor({ timeout: 25000 });
    await page.getByRole('button', { name: '重试加载画板', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('[data-editor-status="ready"]'));
    await page.evaluate(() => ketcher.setMolecule('CCO'));
    assert.equal(await page.evaluate(() => ketcher.getSmiles()), 'CCO');
    assert.ok(requests.some(url => /\/retry\/\d+\//.test(url)));
    // Removing a hung stylesheet may cancel its request entirely. Both a
    // cancelled request and a late response must leave the new session intact.
    if (name.includes('stylesheet')) assert.equal(await page.evaluate(url => [...document.querySelectorAll('link[rel=stylesheet]')].some(link => link.href === url), original), false);
    release();
    if (!name.includes('stylesheet')) await originalDone;
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 250)));
    assert.equal(await page.evaluate(() => ketcher.getSmiles()), 'CCO');
    assert.equal(await page.evaluate(() => window.__workers.active), 1);
    assert.equal(page.workers().length, 1);
  } finally { release(); }
});
await check('url-import-reaches-shared-workspace', async page => {
  await page.goto(base + '/structure-workbench?moll=' + encodeURIComponent('PEPTIDE1{A.G.C}$$$$V2.0'), { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelector('[data-editor-status="ready"]'));
  assert.equal(await page.evaluate(() => ketcher.getSequence()), 'AGC');
  assert.ok(await page.evaluate(() => ketcher.editor.struct().atoms.size > 0));
  await page.getByTestId('text').click();
  assert.equal(await page.evaluate(() => window.__workers.created), 1);
});
await writeFile(resolve(output, 'results.json'), JSON.stringify({ base, browser: browser.version(), cases, passed: cases.every(item => item.passed) }, null, 2));
await browser.close();
if (cases.some(item => !item.passed)) process.exitCode = 1;

import { pollBrowser } from "./browser-poll.mjs";
// Run against ketcher-compat.html only. No business APIs or host application involved.
import assert from 'node:assert/strict';
import { mkdir, writeFile, readFile, readdir, stat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.STRUCTURE_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.KETCHER_COMPAT_URL || 'http://127.0.0.1:4187/ketcher-compat.html';
assert.equal(new URL(base).hostname, '127.0.0.1');
const output = resolve(process.env.KETCHER_COMPAT_ARTIFACTS || '../docs/verification');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.STRUCTURE_CHROMIUM_PATH || undefined,
  headless: true, args: ['--no-sandbox', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
const renderWarnings = [];
page.on('console', message => { if (/Cannot update a component|Cannot update .*while rendering|unmounted root|not wrapped in act/.test(message.text())) renderWarnings.push(message.text()); });
page.on('pageerror', error => errors.push(error.message));
const cdp = await page.context().newCDPSession(page);
await cdp.send('Performance.enable');
const hash = content => createHash('sha256').update(content).digest('hex');
const patchManifest = JSON.parse(await readFile(new URL('../patches/ketcher-manifest.json', import.meta.url), 'utf8'));
const report = { engine: 'react', patch: patchManifest.revision, patchHashes: patchManifest.patches, lockSha256: hash(await readFile(new URL('../package-lock.json', import.meta.url))), sdk: '3.8.0', react: '19', node: process.version, url: base, errors, renderWarnings, samples: [] };
async function measure() {
  await cdp.send('Runtime.discardConsoleEntries');
  await cdp.send('HeapProfiler.collectGarbage');
  const { metrics } = await cdp.send('Performance.getMetrics');
  const sample = { ...Object.fromEntries(metrics.filter(m => ['JSHeapUsedSize', 'Documents', 'Nodes', 'JSEventListeners'].includes(m.name)).map(m => [m.name, m.value])) };
  for (const target of ['window', 'document']) {
    const { result } = await cdp.send('Runtime.evaluate', { expression: target });
    const { listeners } = await cdp.send('DOMDebugger.getEventListeners', { objectId: result.objectId });
    sample[target] = listeners.reduce((counts, listener) => { counts[listener.type] = (counts[listener.type] || 0) + 1; return counts; }, {});
    // CDP exposes handler RemoteObjects; release them or the probe itself retains
    // every measured editor, making DOM/heap growth look like an SDK leak.
    for (const listener of listeners) for (const key of ['handler', 'originalHandler']) {
      if (listener[key]?.objectId) await cdp.send('Runtime.releaseObject', { objectId: listener[key].objectId });
    }
    await cdp.send('Runtime.releaseObject', { objectId: result.objectId });
  }
  sample.workers = page.workers().length;
  sample.retainedEditors = await page.evaluate(() => (window.__retiredEditors || []).filter(ref => ref.deref()).length);
  return sample;
}
try {
  const start = Date.now();
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => Boolean(window.__compatEditor), null, { timeout: 30000 });
  report.readyMs = Date.now() - start;
  report.polyfills = await page.evaluate(() => window.__compatPolyfills());
  assert.deepEqual(report.polyfills, { assertion: true, value: 'util-nextTick', hasGlobalProcess: false });
  report.hostHelpShortcut = await page.evaluate(() => {
    const original = window.open;
    let opened = 0;
    window.open = () => { ++opened; return { focus() {} }; };
    const host = document.createElement('button');
    document.body.append(host);
    const event = new KeyboardEvent('keydown', { key: '?', bubbles: true, cancelable: true });
    try {
      host.dispatchEvent(event);
      return { opened, prevented: event.defaultPrevented };
    } finally { host.remove(); window.open = original; }
  });
  assert.deepEqual(report.hostHelpShortcut, { opened: 0, prevented: false });
  report.assertionFailure = await page.evaluate(async () => {
    const ketcher = window.__compatEditor;
    let failed = 0;
    const failure = () => ++failed;
    ketcher.eventBus.on('FAILURE', failure);
    await ketcher.setMolecule(42);
    ketcher.eventBus.off('FAILURE', failure);
    return failed;
  });
  assert.equal(report.assertionFailure, 1);
  await page.evaluate(() => window.__compatEditor.setMolecule('CCO'));
  await pollBrowser(page, async () => (await window.__compatEditor.getSmiles()) === 'CCO');
  await page.getByTestId('text').click();
  await page.mouse.click(480, 300);
  await page.getByTestId('text-editor').fill('NexPoly native label');
  await page.getByRole('button', { name: 'Apply', exact: true }).click();
  await pollBrowser(page, async () => (await window.__compatEditor.getKet()).includes('NexPoly native label'));
  report.textTool = true;
  const savedKet = await page.evaluate(() => window.__compatEditor.getKet());
  await page.getByTestId('3D Viewer button').click();
  await page.getByText('Miew', { exact: true }).waitFor();
  await page.waitForFunction(() => Boolean(document.querySelector('canvas')));
  // Miew mounts its canvas before conversion/loading finishes. Its Apply
  // control becomes enabled only after the molecule and renderer are ready.
  await page.waitForFunction(() => document.querySelector('[data-testid="miew-modal-button"]')?.disabled === false);
  report.miew3D = true;
  await page.screenshot({ path: resolve(output, 'ketcher-native-tools.png') });
  for (let cycle = 0; cycle <= 10; ++cycle) {
    await page.evaluate(() => window.__compatRemount());
    await page.waitForFunction(() => Boolean(window.__compatEditor), null, { timeout: 30000 });
    await page.evaluate(ket => window.__compatEditor.setMolecule(ket), savedKet);
    await pollBrowser(page, async () => (await window.__compatEditor.getSmiles()) === 'CCO' && (await window.__compatEditor.getKet()).includes('NexPoly native label'));
    // Allow official asynchronous cleanup to finish before collecting garbage.
    await page.waitForTimeout(500);
    report.samples.push({ cycle, ...await measure() });
  }
  report.parallelExports = await page.evaluate(async () => {
    const ketcher = window.__compatEditor;
    const source = await ketcher.getKet();
    const [smiles, ket, mol, png] = await Promise.all([ketcher.getSmiles(), ketcher.getKet(), ketcher.getMolfile('v2000'), ketcher.generateImage(source, { outputFormat: 'png' })]);
    return { smiles, ket: !!JSON.parse(ket).root, mol: /V2000|V3000/.test(mol), png: png instanceof Blob && png.type === 'image/png' && png.size > 100 };
  });
  assert.deepEqual(report.parallelExports, { smiles: 'CCO', ket: true, mol: true, png: true });
  await page.evaluate(() => {
    const old = window.__compatEditor;
    window.__lateCallback = window.__compatService.worker.onmessage;
    window.__compatService.worker.postMessage = () => {};
    window.__oldSettled = null;
    Promise.allSettled([old.getSmiles(), window.__compatService.convert({ struct: 'CCO', output_format: 'chemical/x-indigo-ket' }), window.__compatService.convert({ struct: 'CCO', output_format: 'chemical/x-mdl-molfile' }), old.setMolecule('CCN')]).then(results => { window.__oldSettled = results.map(result => result.status); });
    window.__compatRemount();
  });
  await page.waitForFunction(() => Boolean(window.__compatEditor && window.__oldSettled));
  report.oldRequests = await page.evaluate(async () => {
    await window.__compatEditor.setMolecule('O=C=O');
    window.__lateCallback({ data: { type: 1, hasError: false, inputData: 'CCO', payload: 'OLD' } });
    delete window.__lateCallback;
    return { settled: window.__oldSettled, smiles: await window.__compatEditor.getSmiles() };
  });
  assert.equal(report.oldRequests.smiles, 'O=C=O');
  assert.equal(report.oldRequests.settled.length, 4);
  assert(report.oldRequests.settled.every(status => status === 'rejected'));
  await page.getByRole('textbox', { name: 'Host input' }).fill('host typing');
  await page.keyboard.press('Control+a');
  await page.keyboard.type('host shortcuts');
  assert.equal(await page.getByRole('textbox', { name: 'Host input' }).inputValue(), 'host shortcuts');
  assert.equal(await page.evaluate(() => window.__compatEditor.getSmiles()), 'O=C=O');
  report.hostInput = true;
  await page.evaluate(() => { window.__compatToggle(); delete window.ketcher; });
  await page.waitForTimeout(1500);
  report.unmounted = await measure();
  report.initialization = await page.evaluate(() => ({ count: window.__compatInitCount, errors: window.__compatErrors.length }));
  await page.evaluate(() => { window.__compatUnmountOnCreate = true; window.__compatToggle(); });
  await page.waitForTimeout(600);
  assert.equal(await page.evaluate(() => window.__compatInitCount), report.initialization.count);
  assert.equal(page.workers().length, 0);
  await page.evaluate(() => { window.__compatFailNext = true; window.__compatToggle(); });
  await page.waitForFunction(() => window.__compatErrors.some(error => error.includes('Expected initialization failure')));
  assert.equal(page.workers().length, 0);
  await page.evaluate(() => window.__compatRemount());
  await page.waitForFunction(() => Boolean(window.__compatEditor));
  await page.evaluate(() => window.__compatEditor.setMolecule('CCN'));
  assert.equal(await page.evaluate(() => window.__compatEditor.getSmiles()), 'CCN');
  report.initialization.unmountAndRetry = true;
  await page.evaluate(() => { window.__compatToggle(); delete window.__compatService; });
  await page.waitForTimeout(500);
  report.afterRetry = await measure();
  const first = report.samples[0], last = report.samples.at(-1);
  report.sdkErrors = await page.evaluate(() => window.__compatErrors.filter(error => !error.includes('Expected initialization failure') && !error.includes('AbortError')));
  report.gateFailures = [];
  if (report.sdkErrors.length) report.gateFailures.push('Unexpected SDK error handler calls');
  for (const [target, event] of [['window', 'resize'], ['document', 'mousemove'], ['document', 'mouseup'], ['document', 'mouseleave']]) {
    if ((last[target][event] || 0) > (first[target][event] || 0)) report.gateFailures.push(`${target}.${event}: ${first[target][event]} -> ${last[target][event]} after ten remounts`);
  }
  if (errors.length) report.gateFailures.push('Uncaught browser errors');
  if (renderWarnings.length) report.gateFailures.push('React rendering warnings');
  if (last.retainedEditors) report.gateFailures.push('Retired editor remains reachable');
  if (last.workers !== 1 || report.unmounted.workers !== 0 || report.afterRetry.workers !== 0) report.gateFailures.push('Unexpected active Worker count');
  report.passed = report.gateFailures.length === 0;
} catch (error) {
  report.passed = false;
  report.failure = error.stack;
} finally {
  const mode = process.env.KETCHER_COMPAT_MODE || (new URL(base).port === '5188' ? 'development' : 'production');
  report.mode = mode;
  await writeFile(resolve(output, `ketcher-native-${mode}.json`), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  await browser.close();
  if (!report.passed) process.exitCode = 1;
}

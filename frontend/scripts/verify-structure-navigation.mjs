// Real native editor regressions for interrupted navigation. Business APIs are
// local fixtures; the delayed exports below never change SDK/Worker code.
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const { chromium } = await import(process.env.STRUCTURE_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.STRUCTURE_BASE_URL || 'http://127.0.0.1:5288';
assert.equal(new URL(base).hostname, '127.0.0.1');
const output = resolve(process.env.STRUCTURE_ARTIFACT_DIR || '/tmp/nexpoly-structure-navigation');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.STRUCTURE_CHROMIUM_PATH || undefined,
  headless: true, args: ['--no-sandbox', '--enable-unsafe-swiftshader'] });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'no-preference' });
await context.route('**/*', route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== new URL(base).origin && !['blob:', 'data:'].includes(url.protocol)) return route.abort();
  if (url.pathname.endsWith('/structure/standardize-smiles')) {
    const { smiles } = request.postDataJSON();
    return smiles === 'CC(' ? route.fulfill({ status: 422, json: { detail: 'Invalid fixture draft' } })
      : route.fulfill({ json: { standardized_smiles: smiles } });
  }
  if (url.pathname.startsWith('/api/')) return route.fulfill({ status: 503, json: { detail: 'Read-only navigation fixture' } });
  return ['GET', 'HEAD'].includes(request.method()) ? route.continue() : route.abort();
});
const page = await context.newPage();
page.setDefaultTimeout(30000);
const result = { base, browser: browser.version(), motion: 'normal and reduced', errors: [], checks: [] };
page.on('pageerror', error => result.errors.push(error.message));
await page.addInitScript(() => {
  window.__navNotices = [];
  const observer = new MutationObserver(() => {
    const text = document.querySelector('.np-structure-notice')?.textContent;
    if (text && window.__navNotices.at(-1) !== text) window.__navNotices.push(text);
  });
  observer.observe(document, { childList: true, subtree: true, characterData: true });
});
const input = () => page.getByRole('textbox', { name: 'SMILES 输入，自动同步到画板' });
const choose = module => page.locator(`.np-sidebar-desktop [data-module-id="${module}"]`).click();
const current = () => page.locator('[data-module-content]').getAttribute('data-module-content');
async function ready(module) {
  await page.waitForFunction(module => document.querySelector('[data-module-content]')?.dataset.moduleContent === module &&
    document.querySelector('[data-module-content]')?.dataset.modulePhase === 'idle' &&
    document.querySelector('[data-structure-editor]')?.dataset.editorStatus === 'ready', module);
  assert.equal(await page.locator('[data-structure-editor]').count(), 1);
  assert.equal(page.workers().length, 1);
}
async function read() {
  return page.evaluate(async () => {
    const sdk = window.ketcher, ket = JSON.parse(await sdk.getKet());
    const molecules = Object.values(ket).filter(node => node?.type === 'molecule');
    const origin = molecules[0]?.atoms?.[0]?.location || [0, 0];
    return { smiles: await sdk.getSmiles(), atoms: molecules.reduce((n, node) => n + node.atoms.length, 0),
      geometry: molecules.map(node => node.atoms.map(atom => [atom.label, ...atom.location.slice(0, 2).map((v, i) => Math.round((v - origin[i]) * 1000) / 1000)])),
      labels: ket.root.nodes.filter(node => node.type === 'text').map(node => ({
        text: JSON.parse(node.data.content).blocks.map(block => block.text).join('\n'),
        x: Math.round((node.data.position.x - origin[0]) * 1000) / 1000,
        y: Math.round((node.data.position.y - origin[1]) * 1000) / 1000
      })) };
  });
}
async function cleanNotice() {
  assert.deepEqual(await page.evaluate(() => window.__navNotices), []);
}
async function save() {
  await page.getByRole('button', { name: '生成SMILES', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('[data-workbench-tool="sync"]').disabled);
}
async function secondClickAt(first, second, phase, loading = false) {
  await page.evaluate(({ first, second, phase, loading }) => {
    window.__navTrigger = null;
    const observer = new MutationObserver(() => {
      const module = document.querySelector('[data-module-content]');
      const editor = document.querySelector('[data-structure-editor]');
      if (module?.dataset.modulePhase !== phase || (phase !== 'exiting' && module?.dataset.moduleContent !== first) ||
          (loading && editor?.dataset.editorStatus !== 'loading')) return;
      observer.disconnect();
      window.__navTrigger = { phase, status: editor?.dataset.editorStatus, module: module.dataset.moduleContent };
      document.querySelector(`.np-sidebar-desktop [data-module-id="${second}"]`).click();
    });
    observer.observe(document.body, { childList: true, subtree: true, attributes: true,
      attributeFilter: ['data-module-content', 'data-module-phase', 'data-editor-status'] });
  }, { first, second, phase, loading });
  await choose(first);
  await page.waitForFunction(() => window.__navTrigger !== null);
  await ready(second);
  await cleanNotice();
  return page.evaluate(() => window.__navTrigger);
}
async function holdExport() {
  await page.evaluate(() => {
    const sdk = window.ketcher;
    window.__retainedSdk = sdk;
    window.__heldKetCalls = 0;
    window.__releaseKet = null;
    const original = sdk.getKet.bind(sdk);
    sdk.getKet = async (...args) => {
      const call = ++window.__heldKetCalls;
      const value = await original(...args);
      if (call === 1) return new Promise(resolve => { window.__releaseKet = () => resolve(value); });
      return value;
    };
    window.__restoreKet = () => { sdk.getKet = original; };
  });
}
try {
  await page.goto(`${base}/structure-workbench`, { waitUntil: 'domcontentloaded' });
  await ready('structureWorkbench');
  const groups = page.locator('.np-sidebar-desktop .np-sidebar-group__trigger[aria-expanded="false"]:not(:disabled)');
  while (await groups.count()) await groups.first().click();
  await input().fill('CCO');
  await page.waitForFunction(() => !document.querySelector('[data-workbench-tool="3d"]').disabled);
  await save();
  await page.locator('.np-sidebar-desktop [data-module-id="databaseQuery"]').dblclick({ delay: 80 });
  await ready('databaseQuery');
  await cleanNotice();
  result.checks.push({ name: 'same-target-double-click', smiles: (await read()).smiles });
  await page.evaluate(() => {
    for (const module of ['explorer', 'homopolymerPrediction']) document.querySelector(`.np-sidebar-desktop [data-module-id="${module}"]`).click();
  });
  await ready('homopolymerPrediction');
  await cleanNotice();
  result.checks.push({ name: 'two-targets-before-commit', smiles: (await read()).smiles });
  for (const [first, second, phase, loading] of [
    ['databaseQuery', 'explorer', 'exiting', false],
    ['homopolymerPrediction', 'conditionalGeneration', 'blank', true],
    ['databaseQuery', 'reverseDesign', 'entering', false]
  ]) {
    const trigger = await secondClickAt(first, second, phase, loading);
    assert.equal((await read()).smiles, 'CCO');
    result.checks.push({ name: 'second-target-during-transition', trigger, destination: second });
  }
  // A representative polymer, independent of the molecule in the user's image.
  const polymer = '*' + 'OCCOC(=O)c1ccc(cc1)C(=O)'.repeat(5) + '*';
  await input().fill(polymer);
  await page.waitForFunction(() => !document.querySelector('[data-workbench-tool="3d"]').disabled);
  await page.getByTestId('text').click();
  const box = await page.locator('[data-structure-editor]').boundingBox();
  await page.mouse.click(box.x + box.width * .55, box.y + box.height * .25);
  await page.getByTestId('text-editor').fill('Navigation layout label');
  await page.getByRole('button', { name: 'Apply', exact: true }).click();
  await save();
  const expected = await read();
  assert.ok(expected.atoms > 70 && expected.smiles.includes('*') && expected.labels.length > 0);
  const trigger = await secondClickAt('explorer', 'homopolymerPrediction', 'blank', true);
  assert.deepEqual(await read(), expected);
  result.checks.push({ name: 'polymer-layout-and-annotation', trigger, atoms: expected.atoms, labels: expected.labels.length });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await choose('databaseQuery'); await ready('databaseQuery');
  assert.deepEqual(await read(), expected);
  await cleanNotice();
  result.checks.push({ name: 'reduced-motion-layout' });
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await input().fill('CC(');
  await page.getByText('SMILES 无效或尚未完整，原画板未修改。', { exact: true }).waitFor();
  await secondClickAt('explorer', 'homopolymerPrediction', 'blank', true);
  assert.equal(await input().inputValue(), 'CC(');
  assert.deepEqual(await read(), expected);
  result.checks.push({ name: 'invalid-draft-during-loading' });
  await input().fill('CCO');
  await page.waitForFunction(() => !document.querySelector('[data-workbench-tool="3d"]').disabled);
  await save();

  await holdExport();
  await page.evaluate(() => window.ketcher.setMolecule('CCN'));
  await choose('explorer');
  await page.waitForFunction(() => typeof window.__releaseKet === 'function');
  assert.equal(await page.evaluate(() => window.__heldKetCalls), 1, 'Autosave and navigation must share the same capture');
  await choose('homopolymerPrediction');
  await page.evaluate(() => { window.__restoreKet(); return window.ketcher.setMolecule('CO'); });
  await page.waitForTimeout(1800);
  assert.equal(await current(), 'homopolymerPrediction');
  assert.equal(await page.evaluate(() => window.ketcher === window.__retainedSdk), true);
  await cleanNotice();
  await page.evaluate(() => window.__releaseKet());
  await save();
  assert.equal((await read()).smiles, 'CO');
  result.checks.push({ name: 'cancelled-slow-save-with-later-edit', instanceRetained: true, obsoleteResultIgnored: true });

  // Unlike the no-edit cases, this timeout must still recover a real unsaved
  // edit and display the existing warning, then fence a late export result.
  await holdExport();
  await page.evaluate(() => window.ketcher.setMolecule('CCN'));
  await choose('explorer');
  await page.waitForFunction(() => typeof window.__releaseKet === 'function');
  await ready('explorer');
  assert.ok((await page.locator('.np-structure-notice').textContent()).includes('最新修改未能同步'));
  assert.equal(await page.evaluate(() => window.ketcher === window.__retainedSdk), false);
  await page.evaluate(() => window.__releaseKet());
  assert.equal((await read()).smiles, 'CO');
  result.checks.push({ name: 'real-timeout-still-recovers-and-notifies', lateResultIgnored: true, singleWorker: true });
  await page.screenshot({ path: resolve(output, 'timeout-recovery.png') });
  assert.deepEqual(result.errors, []);
  result.passed = true;
} catch (error) {
  result.passed = false; result.failure = error.stack; process.exitCode = 1;
  await page.screenshot({ path: resolve(output, 'failure.png') }).catch(() => {});
} finally {
  await writeFile(resolve(output, 'result.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
}

// Browser regression for the real native loader under normal module motion.
// All business API requests are fixtures; no jobs or records are submitted.
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { checkDrawerSlide } from './drawer-motion-probe.mjs';
const { chromium } = await import(process.env.STRUCTURE_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.STRUCTURE_BASE_URL || 'http://127.0.0.1:5288';
assert.equal(new URL(base).hostname, '127.0.0.1');
const output = resolve(process.env.STRUCTURE_ARTIFACT_DIR || '/tmp/nexpoly-loading-interactions');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.STRUCTURE_CHROMIUM_PATH || undefined,
  headless: true, args: ['--no-sandbox', '--enable-unsafe-swiftshader'] });
const result = { base, browser: browser.version(), errors: [], expectedFixtureErrors: [], checks: [] };
async function fresh(viewport = { width: 1440, height: 900 }) {
  const context = await browser.newContext({ viewport, reducedMotion: 'no-preference' });
  await context.route('**/*', route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== new URL(base).origin && !['blob:', 'data:'].includes(url.protocol)) return route.abort();
    if (url.pathname.endsWith('/structure/standardize-smiles')) return route.fulfill({ json: { standardized_smiles: request.postDataJSON().smiles } });
    if (url.pathname.endsWith('/monomer-retrosynthesis')) return route.fulfill({ json: { total: 0, candidates: [], input_smiles: 'CCO', target_role: 'auto' } });
    if (url.pathname.startsWith('/api/')) return route.fulfill({ status: 503, json: { detail: 'Read-only browser fixture' } });
    return ['GET', 'HEAD'].includes(request.method()) ? route.continue() : route.abort();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  page.on('pageerror', e => (e.message === 'Injected worker startup failure' ? result.expectedFixtureErrors : result.errors).push(e.message));
  await page.addInitScript(() => {
    window.__loadingEvents = [];
    const NativeWorker = window.Worker;
    window.Worker = new Proxy(NativeWorker, { construct(Target, args) {
      const state = document.querySelector('[data-module-content]');
      window.__loadingEvents.push({ event: 'worker', time: performance.now(), module: state?.dataset.moduleContent, phase: state?.dataset.modulePhase });
      return new Target(...args);
    } });
    const sample = () => {
      const module = document.querySelector('[data-module-content]');
      const root = document.querySelector('[data-structure-editor]');
      const sdk = window.ketcher;
      if (window.__blockedArm === module?.dataset.moduleContent && module?.dataset.modulePhase === 'entering' && root?.dataset.editorStatus === 'ready') {
        const area = sdk.editor.render.paper.canvas;
        const before = sdk.editor.struct().atoms.size;
        for (const data of [{ key: 'a', code: 'KeyA', keyCode: 65, ctrlKey: true }, { key: 'Delete', code: 'Delete', keyCode: 46 }]) {
          area.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...data }));
        }
        const clipboardData = new DataTransfer();
        clipboardData.setData('text/plain', 'CCN');
        area.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData }));
        area.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, button: 2 }));
        const popup = [...document.querySelectorAll('[data-testid="Edit...-option"]')].some(e => e.getClientRects().length);
        window.__blockedResult = { before, after: sdk.editor.struct().atoms.size, locked: module.hasAttribute('inert'), popup,
          selection: sdk.editor.selection()?.atoms?.length || 0, phase: module.dataset.modulePhase };
        window.__blockedArm = null;
      }
      requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  });
  return { page, context };
}
async function expand(page) {
  // The bootstrap now imports App asynchronously after DOMContentLoaded.
  await page.locator('.np-sidebar-desktop .np-sidebar__modules').waitFor();
  const groups = page.locator('.np-sidebar-desktop .np-sidebar-group__trigger[aria-expanded="false"]:not(:disabled)');
  while (await groups.count()) await groups.first().click();
}
async function choose(page, module) { await page.locator(`.np-sidebar-desktop [data-module-id="${module}"]`).click(); }
async function settled(page, module, editor = true) {
  await page.waitForFunction(({ module, editor }) => document.querySelector('[data-module-content]')?.dataset.moduleContent === module &&
    document.querySelector('[data-module-content]')?.dataset.modulePhase === 'idle' && (!editor || document.querySelector('[data-structure-editor]')?.dataset.editorStatus === 'ready'), { module, editor });
}
async function smiles(page) { return page.evaluate(() => window.ketcher.getSmiles()); }
try {
  const { page, context } = await fresh();
  // The first canvas is cold; it may finish after the fade, but must start during it.
  let coldRequest;
  await page.route(/\/KetcherReactRuntime[.-]/, async route => {
    coldRequest ||= await page.evaluate(() => ({ time: performance.now(),
      phase: document.querySelector('[data-module-content]')?.dataset.modulePhase }));
    await route.continue();
  });
  await page.goto(`${base}/knowledge`, { waitUntil: 'domcontentloaded' });
  await expand(page);
  await choose(page, 'structureWorkbench');
  await settled(page, 'structureWorkbench');
  const cold = await page.evaluate(() => window.__loadingEvents.find(e => e.event === 'worker'));
  assert.ok(['blank', 'entering'].includes(coldRequest?.phase), JSON.stringify(coldRequest));
  result.checks.push({ name: 'cold-module-start', request: coldRequest, worker: cold });
  await page.unroute(/\/KetcherReactRuntime[.-]/);
  await page.getByRole('textbox', { name: 'SMILES 输入，自动同步到画板' }).fill('CCO');
  await page.waitForFunction(() => !document.querySelector('[data-workbench-tool="3d"]').disabled);
  assert.equal(await smiles(page), 'CCO');
  await page.evaluate(() => { window.__blockedArm = 'databaseQuery'; });
  await choose(page, 'databaseQuery');
  await settled(page, 'databaseQuery');
  const blocked = await page.evaluate(() => window.__blockedResult);
  assert.ok(blocked, 'Must actually exercise the ready-but-inert incoming editor');
  assert.deepEqual(blocked, { before: 3, after: 3, locked: true, popup: false, selection: 0, phase: 'entering' });
  assert.equal(await smiles(page), 'CCO');
  // Prove the event checks are not a permanently disabled editor.
  await page.waitForTimeout(350);
  await page.locator('[data-testid="select-rectangle"]:visible').click();
  const oxygen = await page.evaluate(() => {
    const node = [...window.ketcher.editor.render.paper.canvas.querySelectorAll('text')].find(node => node.textContent === 'O');
    const box = node.getBoundingClientRect();
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  });
  await page.mouse.click(oxygen.x, oxygen.y);
  await page.waitForFunction(() => window.ketcher.editor.selection()?.atoms?.length === 1);
  await page.mouse.click(oxygen.x, oxygen.y, { button: 'right' });
  await page.getByTestId('Edit...-option').waitFor();
  await page.keyboard.press('Escape');
  await page.mouse.click(oxygen.x, oxygen.y);
  await page.keyboard.press('Control+a');
  await page.waitForFunction(() => window.ketcher.editor.selection()?.atoms?.length === 3);
  result.checks.push({ name: 'transition-event-isolation-and-real-hit', blocked });
  // Rapid target replacement must not leave the intermediate instance alive.
  await choose(page, 'explorer');
  await page.waitForFunction(() => document.querySelector('[data-module-content]')?.dataset.modulePhase === 'exiting');
  await page.evaluate(() => {
    for (const module of ['conditionalGeneration', 'reverseDesign']) document.querySelector(`.np-sidebar-desktop [data-module-id="${module}"]`).click();
  });
  await settled(page, 'reverseDesign');
  assert.equal(await smiles(page), 'CCO');
  assert.equal(await page.locator('[data-structure-editor]').count(), 1);
  assert.equal(page.workers().length, 1);
  assert.equal(await page.locator('.np-structure-notice').count(), 0);
  result.checks.push({ name: 'latest-navigation-target', singleWorker: true });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await choose(page, 'structureWorkbench');
  await settled(page, 'structureWorkbench');
  assert.equal(await smiles(page), 'CCO');
  result.checks.push({ name: 'reduced-motion' });
  await page.screenshot({ path: resolve(output, 'interaction-final.png') });
  await context.close();

  const focusCase = await fresh();
  await focusCase.page.addInitScript(() => {
    document.addEventListener('DOMContentLoaded', () => {
      const input = document.createElement('input');
      input.id = 'loading-host-input';
      input.setAttribute('aria-label', 'Host focus fixture');
      input.style.cssText = 'position:fixed;top:0;right:0;z-index:99999';
      document.body.append(input);
      input.focus({ preventScroll: true });
    });
  });
  await focusCase.page.goto(`${base}/structure-workbench`, { waitUntil: 'domcontentloaded' });
  await focusCase.page.locator('#loading-host-input').fill('host is editing');
  await settled(focusCase.page, 'structureWorkbench');
  await focusCase.page.waitForTimeout(350);
  assert.deepEqual(await focusCase.page.evaluate(() => ({ focused: document.activeElement.id, value: document.activeElement.value, scrollY })),
    { focused: 'loading-host-input', value: 'host is editing', scrollY: 0 });
  result.checks.push({ name: 'host-input-focus-and-scroll' });
  await focusCase.context.close();

  const staleCase = await fresh();
  let release;
  const transport = new Promise(resolve => { release = resolve; });
  let requested = false;
  await staleCase.page.route(/\/KetcherReactRuntime[.-]/, async route => { requested = true; await transport; await route.continue().catch(() => {}); });
  await staleCase.page.goto(`${base}/knowledge`, { waitUntil: 'domcontentloaded' });
  await expand(staleCase.page);
  await choose(staleCase.page, 'structureWorkbench');
  await staleCase.page.locator('[data-structure-editor][data-editor-status="loading"]').waitFor();
  await staleCase.page.waitForTimeout(100);
  assert.ok(requested, 'SDK transport is blocked in the old initialization');
  await choose(staleCase.page, 'databaseQuery');
  await staleCase.page.waitForFunction(() => document.querySelector('[data-module-content]')?.dataset.moduleContent === 'databaseQuery');
  release();
  await settled(staleCase.page, 'databaseQuery');
  assert.equal(await staleCase.page.locator('[data-structure-editor]').count(), 1);
  assert.equal(staleCase.page.workers().length, 1);
  assert.equal(await staleCase.page.locator('.np-structure-notice').count(), 0);
  result.checks.push({ name: 'late-sdk-transport-after-navigation', singleWorker: true });
  await staleCase.context.close();

  const retryCase = await fresh();
  await retryCase.page.addInitScript(() => {
    const Worker = window.Worker;
    let fail = true;
    window.Worker = new Proxy(Worker, { construct(Target, args) {
      if (fail) { fail = false; throw new Error('Injected worker startup failure'); }
      return new Target(...args);
    } });
  });
  await retryCase.page.goto(`${base}/structure-workbench`, { waitUntil: 'domcontentloaded' });
  await retryCase.page.getByRole('button', { name: '重试加载画板' }).click();
  await settled(retryCase.page, 'structureWorkbench');
  await retryCase.page.getByRole('textbox', { name: 'SMILES 输入，自动同步到画板' }).fill('CCO');
  await retryCase.page.waitForFunction(() => !document.querySelector('[data-workbench-tool="3d"]').disabled);
  assert.equal(await smiles(retryCase.page), 'CCO');
  assert.equal(retryCase.page.workers().length, 1);
  result.checks.push({ name: 'failed-initialization-retry', singleWorker: true });
  await retryCase.context.close();

  for (const viewport of [{ width: 390, height: 844 }, { width: 1024, height: 768 }, { width: 1440, height: 900 }, { width: 2560, height: 1440 }]) {
    const drawerCase = await fresh(viewport);
    const page = drawerCase.page;
    await page.goto(`${base}/structure-workbench`, { waitUntil: 'domcontentloaded' });
    await settled(page, 'structureWorkbench');
    await page.getByRole('textbox', { name: 'SMILES 输入，自动同步到画板' }).fill('CCO');
    await page.waitForFunction(() => !document.querySelector('[data-workbench-tool="3d"]').disabled);
    assert.equal(await smiles(page), 'CCO');
    await page.getByRole('button', { name: '功能参数', exact: true }).click();
    await page.getByRole('button', { name: '设置单体逆合成反推参数' }).click();
    await page.getByRole('button', { name: '运行反推', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.np-sw-drawer')?.dataset.motionPhase === 'open');
    const inline = await page.locator('.np-sw-drawer-layer').getAttribute('data-drawer-mode') === 'inline';
    const options = { workspace: '.np-sw-workspace', drawer: '.np-sw-drawer', inline };
    const close = await checkDrawerSlide(page, { ...options, open: false,
      action: () => page.getByRole('button', { name: '关闭单体反推结果', exact: true }).click() });
    const open = await checkDrawerSlide(page, { ...options, open: true,
      action: () => page.getByRole('button', { name: '展开反推结果', exact: true }).click() });
    assert.equal(await smiles(page), 'CCO');
    assert.equal(page.workers().length, 1);
    await page.screenshot({ path: resolve(output, `drawer-${viewport.width}.png`) });
    result.checks.push({ name: 'drawer-and-editor-retention', viewport, close, open });
    await drawerCase.context.close();
  }
  assert.deepEqual(result.errors, []);
  result.passed = true;
} catch (error) {
  result.passed = false;
  result.failure = error.stack;
  process.exitCode = 1;
} finally {
  await writeFile(resolve(output, 'result.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
}

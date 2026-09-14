// Bootstrap/lazy-page regressions. HMR edits are opt-in and restored in finally.
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
import { pollBrowser } from './browser-poll.mjs';
const base = process.env.STRUCTURE_BASE_URL || 'http://127.0.0.1:5912';
assert.equal(new URL(base).hostname, '127.0.0.1');
const output = resolve(process.env.STRUCTURE_ARTIFACT_DIR || '/tmp/nexpoly-refresh-interactions');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.STRUCTURE_CHROMIUM_PATH || undefined,
  headless: true, args: ['--no-sandbox', '--enable-unsafe-swiftshader'] });
const result = { checks: [], errors: [], passed: false };
const manifest = process.env.REFRESH_CHECK_PRODUCTION_CSS === 'true'
  ? await fetch(base + '/.vite/manifest.json').then(response => response.json()) : undefined;
const contexts = [];
const sourceFile = new URL('../src/components/StructureWorkbenchPage.tsx', import.meta.url);
let original;
async function fresh() {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'no-preference' });
  contexts.push(context);
  // This regression exercises loading and HMR without a running business API.
  // Page-specific SDK/CSS failure routes below take precedence over this fixture.
  await context.route('**/*', route => {
    const request = route.request(), url = new URL(request.url());
    if (['blob:', 'data:'].includes(url.protocol)) return route.continue();
    if (url.origin !== new URL(base).origin) return route.abort();
    if (url.pathname === '/api/v1/structure/standardize-smiles') {
      assert.equal(request.method(), 'POST');
      assert.equal(request.postDataJSON().smiles, 'CCO', 'The refresh probe only standardizes its CCO fixture');
      return route.fulfill({ json: { standardized_smiles: 'CCO' } });
    }
    if (url.pathname.startsWith('/api/')) {
      return route.fulfill({ status: 503, json: { detail: 'Refresh regression fixture: backend unavailable' } });
    }
    return request.method() === 'GET' ? route.continue() : route.abort();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  return { context, page };
}
async function ready(page) {
  await page.waitForFunction(() => document.querySelector('[data-structure-editor]')?.dataset.editorStatus === 'ready' &&
    !document.querySelector('[data-workbench-tool="clear"]')?.disabled &&
    document.querySelector('[data-module-content]')?.dataset.modulePhase === 'idle');
}
async function settled(page) { await page.locator('[data-module-content][data-module-phase="idle"]').waitFor(); }
async function expand(page) {
  await page.locator('.np-sidebar-desktop .np-sidebar__modules').waitFor();
  const collapsed = page.locator('.np-sidebar-desktop .np-sidebar-group__trigger[aria-expanded="false"]:not(:disabled)');
  while (await collapsed.count()) await collapsed.first().click();
}
try {
  for (const [route, heading] of [
    ['/', null],
    ['/knowledge', '知识检索'],
    ['/database-filter', '数据库筛选'],
    ['/monomer-dft', '单体 DFT'],
    ['/database/property-filter', '数据库筛选'],
    ['/conditional-generation/polytao', '聚合物生成']
  ]) {
    const { context, page } = await fresh();
    const requests = [];
    page.on('request', request => requests.push(request.url()));
    page.on('pageerror', error => result.errors.push(error.message));
    await page.goto(base + route, { waitUntil: 'domcontentloaded' });
    await page.locator('[data-module-content]').waitFor();
    if (heading) await page.locator('[data-module-content]')
      .getByRole('heading', { level: 1, name: heading, exact: true }).waitFor({ state: 'visible' });
    await page.waitForTimeout(250);
    assert.equal(requests.some(url => /\/KetcherReactRuntime[.-]|\/node_modules\/\.vite\/deps\/ketcher-(?:core|react|standalone)/.test(url)), false, route);
    assert.equal(page.workers().length, 0);
    if (route === '/database/property-filter') assert.equal(new URL(page.url()).pathname, '/database-filter');
    if (route === '/conditional-generation/polytao') assert.equal(new URL(page.url()).pathname, '/polytao-generation');
    result.checks.push({ name: 'ordinary-entry-no-sdk', route });
    await context.close();
  }
  // The early SDK prefetch and the visible editor must share recovery too.
  const sdkCase = await fresh();
  sdkCase.page.on('pageerror', error => result.errors.push(error.message));
  let sdkAttempts = 0, failSdk = true;
  const sdkEntry = manifest?.['src/components/structure-workbench/KetcherReactRuntime.tsx']?.file;
  if (manifest) assert.ok(sdkEntry, 'The SDK has a public entry namespace in the production manifest');
  // Target the actual dynamic entry, not every shared SDK chunk with a similar
  // name: a failed transitive ESM dependency has separate browser semantics.
  await sdkCase.page.route(url => sdkEntry ? url.pathname === '/' + sdkEntry
    : url.pathname.endsWith('/KetcherReactRuntime.tsx'), route => {
    sdkAttempts++;
    return failSdk ? route.abort('failed') : route.continue();
  });
  await sdkCase.page.goto(base + '/structure-workbench', { waitUntil: 'domcontentloaded' });
  await sdkCase.page.getByRole('button', { name: '重试加载画板' }).waitFor();
  failSdk = false;
  await sdkCase.page.getByRole('button', { name: '重试加载画板' }).click();
  await ready(sdkCase.page);
  assert.ok(sdkAttempts >= 2);
  assert.equal(sdkCase.page.workers().length, 1);
  result.checks.push({ name: 'failed-sdk-prefetch-retry', attempts: sdkAttempts, workers: 1 });
  await sdkCase.context.close();

  if (process.env.REFRESH_CHECK_PRODUCTION_CSS === 'true') {
    const cssCase = await fresh();
    await cssCase.page.goto(base + '/structure-workbench', { waitUntil: 'domcontentloaded' });
    await ready(cssCase.page); await expand(cssCase.page);
    let cssAttempts = 0;
    await cssCase.page.route(/\/assets\/KnowledgeSearch-[^/]+\.css(?:\?|$)/, route => {
      cssAttempts++;
      return cssAttempts === 1 ? route.abort('failed') : route.continue();
    });
    await cssCase.page.locator('.np-sidebar-desktop [data-module-id="knowledge"]').click();
    await cssCase.page.getByRole('button', { name: '重试加载页面' }).click();
    await cssCase.page.getByRole('heading', { name: '知识检索', exact: true }).waitFor();
    assert.ok(cssAttempts >= 2);
    assert.ok(await cssCase.page.evaluate(() => [...document.styleSheets].some(sheet => /KnowledgeSearch-[^/]+\.css/.test(sheet.href || ''))));
    result.checks.push({ name: 'failed-page-css-retry', attempts: cssAttempts });
    await cssCase.context.close();
  }
  const { page, context } = await fresh();
  page.on('pageerror', error => result.errors.push(error.message));
  await page.goto(base + '/structure-workbench', { waitUntil: 'domcontentloaded' });
  await ready(page); await expand(page);
  await page.getByRole('textbox', { name: 'SMILES 输入，自动同步到画板' }).fill('CCO');
  await page.waitForFunction(() => !document.querySelector('[data-workbench-tool="3d"]').disabled);
  await page.evaluate(() => { window.__retainedEditor = document.querySelector('[data-structure-editor]'); });

  let attempts = 0;
  const target = /\/(?:KnowledgeSearch\.tsx|assets\/KnowledgeSearch-[^/]+\.js)(?:\?|$)/;
  await page.route(target, route => { attempts++; return attempts === 1 ? route.abort('failed') : route.continue(); });
  await page.locator('.np-sidebar-desktop [data-module-id="knowledge"]').click();
  await page.getByRole('button', { name: '重试加载页面' }).click();
  await page.getByRole('heading', { name: '知识检索', exact: true }).waitFor();
  await settled(page);
  assert.ok(attempts >= 2, 'Retry reissues the failed browser module request');
  assert.equal(await page.evaluate(() => document.querySelector('[data-structure-editor]') === window.__retainedEditor), true);
  await page.locator('.np-sidebar-desktop [data-module-id="structureWorkbench"]').click();
  await ready(page);
  assert.equal(await page.evaluate(() => window.ketcher.getSmiles()), 'CCO');
  await page.unroute(target);
  result.checks.push({ name: 'real-module-fetch-retry-preserves-canvas', attempts });

  await page.goBack();
  await page.getByRole('heading', { name: '知识检索', exact: true }).waitFor();
  await settled(page);
  await page.goForward();
  await page.getByRole('heading', { name: '结构工作台', exact: true }).waitFor();
  await ready(page);
  assert.equal(await page.evaluate(() => window.ketcher.getSmiles()), 'CCO');
  assert.equal(await page.evaluate(() => document.querySelector('[data-structure-editor]') === window.__retainedEditor), true);
  result.checks.push({ name: 'history-back-forward-preserves-canvas-and-structure' });

  if (process.env.REFRESH_CHECK_HMR === 'true') {
    const before = await page.evaluate(() => ({ epoch: performance.timeOrigin, sdk: !!window.ketcher }));
    await page.evaluate(() => { window.__hmrSdk = window.ketcher; window.__hmrCanvas = document.querySelector('[data-structure-editor]'); });
    original = await readFile(sourceFile, 'utf8');
    const titleText = />\s*结构工作台\s*<\//g;
    assert.equal([...original.matchAll(titleText)].length, 1, 'HMR probe changes the unique page heading text');
    await writeFile(sourceFile, original.replace(titleText, match => match.replace('结构工作台', '结构工作台 HMR 验证')));
    await page.getByRole('heading', { name: '结构工作台 HMR 验证', exact: true }).waitFor();
    const after = await page.evaluate(() => ({ epoch: performance.timeOrigin, sameSdk: window.__hmrSdk === window.ketcher,
      sameCanvas: window.__hmrCanvas === document.querySelector('[data-structure-editor]') }));
    assert.equal(after.epoch, before.epoch, 'Business edits use HMR without document reload');
    assert.ok(after.sameSdk && after.sameCanvas, 'HMR preserves the existing editor session');
    await pollBrowser(page, async () => (await window.ketcher.getSmiles()) === 'CCO');
    await writeFile(sourceFile, original); original = undefined;
    await page.getByRole('heading', { name: '结构工作台', exact: true }).waitFor();
    result.checks.push({ name: 'business-hmr-preserves-document-and-sdk', ...after });

    await page.reload({ waitUntil: 'domcontentloaded' });
    await ready(page);
    const hints = await page.evaluate(() => {
      const resources = performance.getEntriesByType('resource');
      return [...document.querySelectorAll('link[rel="modulepreload"]')]
        .filter(link => /\/src\/(routing\.ts|pages\.ts|mountApp\.tsx)$/.test(new URL(link.href).pathname))
        .map(link => ({ href: link.href, requests: resources.filter(r => new URL(r.name).pathname === new URL(link.href).pathname).length }));
    });
    result.checks.push({ name: 'bootstrap-preload-reload-after-hmr', hints });
    assert.equal(hints.length, 3, 'HTML discovers all three lightweight bootstrap roots');
    assert.ok(hints.every(hint => hint.requests === 1), 'Reload after HMR reuses the preloaded module URL');
  }
  await context.close();
  assert.deepEqual(result.errors, []);
  result.passed = true;
} catch (error) { result.failure = String(error.stack || error); process.exitCode = 1; }
finally {
  if (original !== undefined) await writeFile(sourceFile, original);
  for (const context of contexts) await context.close().catch(() => {});
  await browser.close();
  await writeFile(resolve(output, 'result.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
}

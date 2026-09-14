// Deep-link delivery gate for both engines. Only local SDK editing is performed.
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
import { pollBrowser } from './browser-poll.mjs';
const base = process.env.STRUCTURE_BASE_URL || 'http://127.0.0.1:9001';
assert.equal(new URL(base).hostname, '127.0.0.1');
const output = resolve(process.env.STRUCTURE_ARTIFACT_DIR || '/tmp/nexpoly-structure-entries');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.STRUCTURE_CHROMIUM_PATH || undefined,
  headless: true, args: ['--no-sandbox', '--enable-unsafe-swiftshader'] });
const results = [];
try {
  for (const route of ['/structure-workbench', '/database-query', '/explorer', '/homopolymer-property-prediction', '/conditional-generation', '/reverse-design']) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'no-preference' });
    const page = await context.newPage();
    page.setDefaultTimeout(45000);
    const item = { route, passed: false, errors: [], requests: [] };
    results.push(item);
    page.on('pageerror', error => item.errors.push(error.message));
    page.on('request', request => item.requests.push(request.url()));
    await context.addInitScript(() => {
      window.__entrySdk = () => document.querySelector('[data-structure-editor] iframe')?.contentWindow?.ketcher || window.ketcher;
    });
    try {
      await page.goto(base + route, { waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => {
        const root = document.querySelector('[data-structure-editor]');
        const canvas = window.__entrySdk()?.editor?.render?.paper?.canvas;
        return root?.dataset.editorStatus === 'ready' && canvas?.getBoundingClientRect().width > 0 &&
          document.querySelector('[data-workbench-tool="clear"]')?.disabled === false;
      });
      item.engine = await page.locator('[data-structure-editor]').getAttribute('data-editor-engine');
      item.sdk = await page.evaluate(() => window.__entrySdk().version);
      await page.waitForTimeout(350);
      await page.evaluate(() => window.__entrySdk().setMolecule('CCO'));
      await page.waitForFunction(() => window.__entrySdk().editor.struct().atoms.size === 3);
      const ui = item.engine === 'iframe' ? page.frameLocator('[data-structure-editor] iframe') : page;
      await ui.locator('[data-testid="select-rectangle"]:visible').click();
      const before = await page.evaluate(async () => {
        const sdk = window.__entrySdk();
        sdk.editor.selection(null);
        const oxygen = [...sdk.editor.render.paper.canvas.querySelectorAll('text')].find(node => node.textContent === 'O').getBoundingClientRect();
        const iframe = document.querySelector('[data-structure-editor] iframe');
        const frame = iframe?.getBoundingClientRect();
        const scale = frame ? frame.width / iframe.contentWindow.innerWidth : 1;
        const molecule = Object.values(JSON.parse(await sdk.getKet())).find(v => v?.type === 'molecule');
        return { x: (frame?.x || 0) + (oxygen.x + oxygen.width / 2) * scale,
          y: (frame?.y || 0) + (oxygen.y + oxygen.height / 2) * scale,
          locations: molecule.atoms.map(atom => atom.location) };
      });
      await page.mouse.move(before.x, before.y); await page.mouse.down();
      await page.mouse.move(before.x + 50, before.y - 25, { steps: 5 }); await page.mouse.up();
      await pollBrowser(page, async previous => {
        const molecule = Object.values(JSON.parse(await window.__entrySdk().getKet())).find(v => v?.type === 'molecule');
        return JSON.stringify(molecule.atoms.map(atom => atom.location)) !== JSON.stringify(previous);
      }, before.locations);
      item.smiles = await page.evaluate(() => window.__entrySdk().getSmiles());
      assert.equal(item.smiles, 'CCO');
      await page.getByRole('button', { name: '清空画布', exact: true }).click();
      await pollBrowser(page, async () => (await window.__entrySdk().getSmiles()) === '');
      const native = item.requests.some(url => /KetcherReactRuntime[.-]/.test(url));
      const iframe = item.requests.some(url => new URL(url).pathname === '/ketcher/index.html');
      assert.equal(native, item.engine === 'react');
      assert.equal(iframe, item.engine === 'iframe');
      assert.deepEqual(item.errors, []);
      item.passed = true;
      item.realAtomDrag = true; item.clear = true; item.resourceIsolation = true;
    } catch (error) {
      item.failure = String(error.stack || error);
      await page.screenshot({ path: resolve(output, `failure-${route.slice(1)}.png`) }).catch(() => {});
    } finally { await context.close(); }
    console.log(JSON.stringify({ ...item, requests: item.requests.length }));
    await writeFile(resolve(output, 'result.json'), JSON.stringify({ base, results, passed: results.every(r => r.passed) }, null, 2));
  }
} finally { await browser.close(); }
if (results.some(r => !r.passed)) process.exitCode = 1;

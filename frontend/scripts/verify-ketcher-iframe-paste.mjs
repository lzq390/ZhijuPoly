// Real SDK regression: hold the paste's merge timers, enter and leave the
// canvas, then release it. No business APIs, scientific jobs or wall-clock sleeps.
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const { chromium } = await import(process.env.STRUCTURE_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.STRUCTURE_BASE_URL || 'http://127.0.0.1:5187';
assert.equal(new URL(base).hostname, '127.0.0.1');
const output = resolve(process.env.STRUCTURE_ARTIFACT_DIR || '/tmp/nexpoly-iframe-paste');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.STRUCTURE_CHROMIUM_PATH || undefined,
  headless: true, args: ['--no-sandbox', '--enable-unsafe-swiftshader'] });
const report = { errors: [] };
let page;
try {
  const context = await browser.newContext({ viewport: { width: 1200, height: 800 } });
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base });
  await context.route('**/*', route => {
    const request = route.request(), url = new URL(request.url());
    return url.origin === new URL(base).origin && ['GET', 'HEAD'].includes(request.method())
      ? route.continue() : route.abort();
  });
  page = await context.newPage();
  page.on('pageerror', error => report.errors.push(error.stack || error.message));
  await page.goto(base + '/ketcher/index.html');
  await page.waitForFunction(() => Boolean(window.ketcher?.editor));
  // Serialize initial imports and exports. This standalone page has no host
  // controlled value, autosave or application reconciliation callbacks.
  await page.evaluate(() => window.ketcher.setMolecule('CCN'));
  await page.waitForFunction(() => window.ketcher.editor.struct().atoms.size === 3);
  await page.evaluate(async () => navigator.clipboard.writeText(await window.ketcher.getMolfile()));
  await page.evaluate(() => window.ketcher.setMolecule(''));
  await page.waitForFunction(() => window.ketcher.editor.struct().atoms.size === 0);
  const point = await page.evaluate(() => {
    const rect = window.ketcher.editor.render.clientArea.getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
  });
  await page.mouse.click(point.x, point.y);
  // Clipboard import can briefly place UI under an unmoved pointer. Start
  // outside so the later leave has an observed, deliberate canvas entry.
  await page.mouse.move(5, 5);
  await page.evaluate(() => {
    const editor = window.ketcher.editor, tool = editor.tool;
    const nativeTimeout = window.setTimeout.bind(window), nativeClear = window.clearTimeout.bind(window);
    const nativeNow = Date.now.bind(Date), pending = new Map();
    let capturing = false, id = 1000000000, advance = 0;
    window.__pasteRegression = { ready: 0, canceled: 0, moves: 0, events: [] };
    for (const type of ['mouseover', 'mousemove', 'mouseleave']) {
      editor.render.clientArea.addEventListener(type, event => {
        const events = window.__pasteRegression.events;
        events.push({ type, trusted: event.isTrusted, target: event.target.nodeName,
          x: event.clientX, y: event.clientY });
        if (events.length > 12) events.shift();
      });
    }
    window.addEventListener('itemsToMergeInitializationComplete', () => window.__pasteRegression.ready++);
    Date.now = () => nativeNow() + advance;
    window.setTimeout = function (callback, delay, ...args) {
      if (capturing && delay === 50) {
        const key = ++id;
        pending.set(key, () => callback(...args));
        return key;
      }
      return nativeTimeout(callback, delay, ...args);
    };
    window.clearTimeout = key => { if (!pending.delete(key)) nativeClear(key); };
    editor.tool = function (...args) {
      capturing = args[0] === 'paste';
      try { return tool.apply(this, args); }
      finally {
        capturing = false;
        if (args[0] === 'paste') {
          const paste = this._tool, cancel = paste.cancel, move = paste.mousemove;
          paste.cancel = function (...values) {
            window.__pasteRegression.canceled++;
            return cancel.apply(this, values);
          };
          paste.mousemove = function (...values) {
            capturing = true;
            try {
              const result = move.apply(this, values);
              window.__pasteRegression.moves++;
              return result;
            } finally { capturing = false; }
          };
        }
      }
    };
    window.__pasteRegression.snapshot = () => ({
      atoms: editor.struct().atoms.size, action: Boolean(editor._tool.action),
      pasteTool: Boolean(editor._tool.struct?.atoms), pending: pending.size,
      ready: window.__pasteRegression.ready, canceled: window.__pasteRegression.canceled,
      moves: window.__pasteRegression.moves, events: window.__pasteRegression.events.slice(),
      canvas: (() => {
        const area = editor.render.clientArea, rect = area.getBoundingClientRect();
        return { connected: area.isConnected, hovered: area.matches(':hover'),
          x: rect.x, y: rect.y, width: rect.width, height: rect.height };
      })()
    });
    window.__pasteRegression.release = () => {
      // Lodash checks elapsed time again when its timer fires. Advance this
      // page's clock before delivering exactly the captured merge callbacks.
      advance += 1000;
      const work = [...pending.values()];
      pending.clear();
      window.__pasteRegression.released = 0;
      window.__pasteRegression.expected = work.length;
      for (const callback of work) nativeTimeout(() => {
        try { callback(); } finally { window.__pasteRegression.released++; }
      }, 0);
    };
  });
  await page.keyboard.press('Control+v');
  await page.waitForFunction(() => Boolean(window.ketcher.editor._tool?.struct?.atoms));
  report.preview = await page.evaluate(() => window.__pasteRegression.snapshot());
  assert.equal(report.preview.atoms, 3);
  assert.equal(report.preview.action, true);
  assert.equal(report.preview.pending, 1);
  assert.equal(report.preview.ready, 0);
  await page.evaluate(() => { window.__pasteRegression.initialAction = window.ketcher.editor._tool.action; });
  const placementHandle = await page.waitForFunction(() => {
    const area = window.ketcher.editor.render.clientArea, rect = area.getBoundingClientRect();
    const point = { x: rect.x + rect.width / 2 + 1, y: rect.y + rect.height / 2 };
    const hit = document.elementFromPoint(point.x, point.y);
    return area.isConnected && area.contains(hit) && hit.nodeName !== 'DIV' && point;
  });
  const placement = await placementHandle.jsonValue();
  await placementHandle.dispose();
  await page.mouse.move(placement.x, placement.y);
  await page.waitForFunction(() => {
    const state = window.__pasteRegression, editor = window.ketcher.editor;
    return state.moves > 0 && editor._tool.action && editor._tool.action !== state.initialAction &&
      editor.render.clientArea.matches(':hover');
  });
  report.placedPreview = await page.evaluate(() => window.__pasteRegression.snapshot());
  assert.equal(report.placedPreview.pending, 1);
  assert.equal(report.placedPreview.ready, 0);
  // A real mouseLeaveClientArea event must roll back this preview.
  await page.mouse.move(5, 5);
  await page.waitForFunction(() => window.__pasteRegression.canceled > 0 &&
    !window.ketcher.editor._tool.action && window.ketcher.editor.struct().atoms.size === 0);
  report.canceled = await page.evaluate(() => window.__pasteRegression.snapshot());
  assert.equal(report.canceled.atoms, 0);
  assert.equal(report.canceled.action, false);
  assert.equal(report.canceled.pasteTool, true);
  assert.ok(report.canceled.canceled > 0);
  await page.evaluate(() => window.__pasteRegression.release());
  await page.waitForFunction(() => window.__pasteRegression.released === window.__pasteRegression.expected);
  assert.deepEqual(report.errors, [], 'Canceled paste merge callback read rolled-back preview atoms');
  assert.equal(report.canceled.pending, 0, 'Cancel left the initial merge callback pending');
  await page.mouse.move(placement.x, placement.y);
  await page.waitForFunction(() => window.__pasteRegression.moves > 1 && Boolean(window.ketcher.editor._tool.action));
  // Release this new, valid merge callback after proving cancellation disposed
  // the old one. Reentry is still a real SDK mousemove, with no synthetic events.
  await page.evaluate(() => window.__pasteRegression.release());
  await page.waitForFunction(() => window.__pasteRegression.released === window.__pasteRegression.expected);
  await page.waitForFunction(() => window.__pasteRegression.ready === 1 && Boolean(window.ketcher.editor._tool.action));
  report.resumed = await page.evaluate(() => window.__pasteRegression.snapshot());
  await page.mouse.down();
  await page.mouse.up();
  report.committed = await page.evaluate(async () => ({
    atoms: window.ketcher.editor.struct().atoms.size, smiles: await window.ketcher.getSmiles(),
    pasteTool: Boolean(window.ketcher.editor._tool.struct?.atoms)
  }));
  assert.deepEqual(report.committed, { atoms: 3, smiles: 'CCN', pasteTool: false });
  assert.deepEqual(report.errors, []);
  report.passed = true;
} catch (error) {
  report.failure = error.stack || error.message;
  try { report.failureSnapshot = await page.evaluate(() => window.__pasteRegression?.snapshot()); } catch { /* Page closed. */ }
  console.error(JSON.stringify(report, null, 2));
  throw error;
} finally {
  await writeFile(resolve(output, 'paste-cancellation.json'), JSON.stringify(report, null, 2) + '\n');
  await browser.close();
}
console.log('Verified real iframe paste cancellation, stale timer disposal and reentry commit.');

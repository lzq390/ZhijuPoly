// Read-only navigation diagnostics; CCO is entered locally and standardized, no jobs are submitted.
// PROBE_OVERLAP is a disposable browser experiment, never a product prototype patch.
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import assert from 'node:assert/strict';
const { chromium } = await import(process.env.PROBE_PLAYWRIGHT_MODULE || 'playwright');
const port = Number(process.env.PROBE_PORT || 9001);
const label = process.env.PROBE_LABEL || String(port);
const base = `http://127.0.0.1:${port}`;
const outputDir = resolve(process.env.PROBE_OUTPUT_DIR || '/tmp/nexpoly-editor-loading-analysis');
await mkdir(outputDir, { recursive: true });
const output = resolve(outputDir, `${label}.json`);
const acceptance = process.env.PROBE_ACCEPTANCE === 'true';
assert.ok(!acceptance || process.env.PROBE_OVERLAP !== 'true', 'Acceptance must run the real implementation, without the overlap experiment');
const browser = await chromium.launch({ executablePath: process.env.PROBE_CHROMIUM_PATH || undefined, headless: true, args: ['--no-sandbox', '--enable-unsafe-swiftshader'] });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'no-preference' });
const page = await context.newPage();
page.setDefaultTimeout(30000);
const warmupRounds = Number(process.env.PROBE_WARMUP_ROUNDS || 0);
assert.ok(Number.isInteger(warmupRounds) && warmupRounds >= 0 && warmupRounds <= 3);
const settings = { viewport: { width: 1440, height: 900 }, reducedMotion: 'no-preference', acceptance, overlapExperiment: process.env.PROBE_OVERLAP === 'true', warmupRounds, dwellMs: Number(process.env.PROBE_DWELL_MS || 150), input: 'CCO', cache: 'normal browser HTTP cache, no route interception', cpuNetworkThrottle: 'none' };
const pageErrors = [];
page.on('pageerror', e => pageErrors.push(e.message));
const network = [];
page.on('response', async r => {
  const url = new URL(r.url());
  if (url.origin !== base || !['script', 'stylesheet', 'document'].includes(r.request().resourceType())) return;
  const headers = await r.allHeaders();
  network.push({ url: url.pathname, status: r.status(), size: headers['content-length'], encoding: headers['content-encoding'], cache: headers['cache-control'], timing: r.request().timing() });
});
await context.addInitScript(() => {
  let trace;
  try {
    if (window === top) window.__loadProbe = { events: [], frames: [], next: 1, epoch: performance.timeOrigin };
    trace = top.__loadProbe;
  } catch { return; }
  if (!trace) return;
  const record = (type, data = {}) => trace.events.push({ t: Math.round((performance.timeOrigin + performance.now() - trace.epoch) * 10) / 10, type, frame: window === top ? 'top' : location.pathname, ...data });
  const NativeWorker = window.Worker;
  window.Worker = new Proxy(NativeWorker, {
    construct(Target, args) {
      const worker = new Target(...args);
      const id = trace.next++;
      record('worker_create', { id });
      const post = worker.postMessage;
      worker.postMessage = function (...params) { record('worker_request', { id, command: params[0]?.cmd || params[0]?.command || params[0]?.type, keys: Object.keys(params[0] || {}).slice(0, 6) }); return post.apply(this, params); };
      worker.addEventListener('message', event => record('worker_reply', { id, command: event.data?.cmd || event.data?.command || event.data?.type }));
      const terminate = worker.terminate;
      worker.terminate = function () { record('worker_terminate', { id }); return terminate.call(this); };
      return worker;
    }
  });
  const known = new WeakMap();
  let nextObject = 1;
  const identify = value => { if (!value) return null; if (!known.has(value)) known.set(value, nextObject++); return known.get(value); };
  let previous = '';
  let lastSdk;
  let lastFrame;
  let geometry;
  const sample = () => {
    const sampleStart = performance.now();
    const sdk = window.ketcher;
    if (sdk && sdk !== lastSdk) {
      lastSdk = sdk;
      record('sdk_available');
      for (const method of ['setMolecule', 'getKet', 'getSmiles', 'getMolfile']) {
        if (typeof sdk[method] !== 'function') continue;
        const fn = sdk[method];
        sdk[method] = function (...args) {
          const operation = trace.next++;
          record('sdk_call', { method, operation, input: method === 'setMolecule' ? (!args[0] ? 'empty' : String(args[0]).trimStart().startsWith('{') ? 'ket' : 'smiles/mol') : undefined });
          const value = fn.apply(this, args);
          if (value?.then) value.then(() => record('sdk_done', { method, operation }), () => record('sdk_rejected', { method, operation }));
          else record('sdk_done', { method, operation });
          return value;
        };
      }
    }
    if (window === top) {
      const content = document.querySelector('[data-module-content]');
      const stage = document.querySelector('.np-sw-canvas-stage');
      const root = stage?.querySelector('[data-structure-editor]') || stage;
      const iframe = root?.querySelector('iframe[src*="ketcher"]');
      const editor = iframe?.contentWindow?.ketcher || window.ketcher;
      let atoms = null;
      try {
        atoms = editor?.editor?.struct?.()?.atoms?.size ?? null;
      } catch {}
      const phase = content?.dataset.modulePhase;
      const status = root?.dataset.editorStatus;
      const geometryKey = [identify(root), identify(editor), content?.dataset.moduleContent, phase, status, atoms, innerWidth, innerHeight, root?.hasAttribute('inert')].join(':');
      // Geometry reads can force layout. Do them at state boundaries and when
      // waiting for the first usable hit, never on every locked animation frame.
      if (geometry?.key !== geometryKey || (phase === 'idle' && (status === undefined || status === 'ready') && !geometry.oxygenHit)) {
        const bounds = root?.getBoundingClientRect();
        geometry = { key: geometryKey, sized: !!bounds && bounds.width > 0 && bounds.height > 0,
          svg: false, oxygenVisible: false, oxygenHit: false };
        try {
          const box = editor?.editor?.render?.paper?.canvas?.getBoundingClientRect();
          geometry.svg = !!box && box.width > 0 && box.height > 0;
          if (phase === 'idle' && (status === undefined || status === 'ready') && atoms === 3 && geometry.svg) {
            const text = [...editor.editor.render.paper.canvas.querySelectorAll('text')].find(node => node.textContent === 'O');
            const atom = text?.getBoundingClientRect();
            const frame = iframe?.getBoundingClientRect();
            const scale = iframe ? frame.width / iframe.contentWindow.innerWidth : 1;
            const x = (frame?.x || 0) + (atom?.x + atom?.width / 2) * scale;
            const y = (frame?.y || 0) + (atom?.y + atom?.height / 2) * scale;
            geometry.oxygenVisible = !!atom?.width && x > Math.max(0, bounds.left) && x < Math.min(innerWidth, bounds.right) &&
              y > Math.max(0, bounds.top) && y < Math.min(innerHeight, bounds.bottom);
            geometry.oxygenHit = geometry.oxygenVisible && root.contains(document.elementFromPoint(x, y));
          }
        } catch {}
      }
      const { sized, svg, oxygenVisible, oxygenHit } = geometry;
      const buttonsReady = !document.querySelector('[data-workbench-tool="3d"]')?.disabled;
      const interactive = !!root && !root.closest('[inert], [hidden], [aria-hidden="true"]') && oxygenHit && buttonsReady;
      const state = { module: content?.dataset.moduleContent, phase, locked: content?.getAttribute('aria-hidden'), root: identify(root), iframe: identify(iframe), sdk: identify(editor), status, sized, atoms, svg, oxygenVisible, oxygenHit, buttonsReady, interactive };
      const time = performance.now();
      if (lastFrame !== undefined) trace.frames.push({ time, delta: time - lastFrame,
        probeDurationMs: performance.now() - sampleStart, module: state.module, phase: state.phase });
      lastFrame = time;
      const signature = JSON.stringify(state);
      if (signature !== previous) { previous = signature; record('state', state); }
      window.__loadState = state;
    }
    requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
  if (window === top) {
    document.addEventListener('click', e => {
      const target = e.target.closest('[data-module-id]');
      if (target) record('navigation_click', { target: target.dataset.moduleId });
    }, true);
    const animate = Element.prototype.animate;
    Element.prototype.animate = function (keyframes, options) {
      const animation = animate.call(this, keyframes, options);
      if (options?.id?.startsWith('np-module-')) {
        record('animation_start', { name: options.id, module: this.dataset.moduleContent, duration: options.duration });
        animation.addEventListener('finish', () => record('animation_finish', { name: options.id, module: this.dataset.moduleContent }));
      }
      return animation;
    };
    new PerformanceObserver(entries => {
      for (const e of entries.getEntries()) record('longtask', { start: e.startTime, duration: e.duration });
    }).observe({ type: 'longtask', buffered: true });
  }
});
if (process.env.PROBE_OVERLAP === 'true') {
  await context.addInitScript(() => {
    const original = Element.prototype.closest;
    Element.prototype.closest = function(selector) {
      if (selector === '[hidden], [aria-hidden="true"]' && this.classList.contains('np-structure-native')) {
        for (let node = this; node; node = node.parentElement) {
          if (node.hasAttribute('hidden')) return node;
          if (node.getAttribute('aria-hidden') === 'true' && !(node.hasAttribute('data-module-content') && node.getAttribute('data-module-transitioning') === 'true')) return node;
        }
        return null;
      }
      return original.call(this, selector);
    };
  });
}
const runs = [];
const warmups = [];
const percentile = (values, fraction) => [...values].sort((a, b) => a - b)[Math.ceil(values.length * fraction) - 1];
let coldLoad;
let summary;
try {
  await page.goto(`${base}/structure-workbench`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.__loadState?.svg && (window.__loadState?.status === undefined || window.__loadState.status === 'ready'));
  coldLoad = await page.evaluate(() => ({ readyMs: performance.now(), events: [...window.__loadProbe.events] }));
  const input = page.getByRole('textbox', { name: 'SMILES 输入，自动同步到画板' });
  await input.fill('CCO');
  await page.waitForFunction(() => window.__loadState?.atoms === 3 && !document.querySelector('[data-workbench-tool="3d"]')?.disabled);
  await page.waitForTimeout(500);
  const groups = page.locator('.np-sidebar-desktop .np-sidebar-group__trigger[aria-expanded="false"]:not(:disabled)');
  while (await groups.count()) await groups.first().click();
  const targets = ['databaseQuery', 'explorer', 'homopolymerPrediction', 'conditionalGeneration', 'reverseDesign', 'structureWorkbench', 'databaseQuery', 'structureWorkbench', 'databaseQuery', 'structureWorkbench', 'knowledge', 'structureWorkbench'];
  // Predeclare the warmup, including KET restoration on every owner. Keep its
  // samples separately; never decide whether to exclude a run from its timing.
  const navigationTargets = [...Array.from({ length: warmupRounds }, () => targets.slice(0, 6)).flat(), ...targets];
  for (const [index, target] of navigationTargets.entries()) {
    const warming = index < warmupRounds * 6;
    const start = await page.evaluate(() => ({ from: window.__loadState.module, t: performance.now(), eventIndex: window.__loadProbe.events.length, previous: window.__loadState }));
    await page.locator(`.np-sidebar-desktop [data-module-id="${target}"]`).click();
    await page.waitForFunction(target => window.__loadState?.module === target && window.__loadState?.phase === 'idle' && (target === 'knowledge' || (window.__loadState?.atoms === 3 && window.__loadState.svg && window.__loadState.interactive && (window.__loadState.status === undefined || window.__loadState.status === 'ready'))), target);
    await page.waitForTimeout(settings.dwellMs);
    const data = await page.evaluate(start => ({ ...start, end: performance.now(), final: window.__loadState, events: window.__loadProbe.events.slice(start.eventIndex) }), start);
    const click = data.events.find(e => e.type === 'navigation_click')?.t ?? start.t;
    const idle = data.events.find(e => e.type === 'state' && e.module === target && e.phase === 'idle');
    const mounted = data.events.find(e => e.type === 'state' && e.module === target);
    const rendered = data.events.find(e => e.type === 'state' && e.module === target && e.atoms === 3 && e.svg);
    const ready = data.events.find(e => e.type === 'state' && e.module === target && e.atoms === 3 && e.svg && (e.status === undefined || e.status === 'ready'));
    const workers = data.events.filter(e => e.type === 'worker_create');
    const interactive = data.events.find(e => e.type === 'state' && e.module === target && e.phase === 'idle' && e.interactive && (e.status === undefined || e.status === 'ready'));
    const run = { from: start.from, target, click, mountMs: mounted ? mounted.t - click : null, idleMs: idle ? idle.t - click : null, atomsMs: rendered ? rendered.t - click : null, readyMs: ready ? ready.t - click : null, interactiveMs: interactive ? interactive.t - click : null, afterAnimationMs: ready && idle ? Math.max(0, ready.t - idle.t) : null, firstWorkerMs: workers[0] ? workers[0].t - click : null, workersCreated: workers.length, retainedRoot: start.previous.root === data.final.root, ...data };
    if (target !== 'knowledge') {
      run.exportedSmiles = await page.evaluate(async () => {
        const iframe = document.querySelector('.np-sw-canvas-stage iframe[src*="ketcher"]');
        return (await (iframe?.contentWindow?.ketcher || window.ketcher).getSmiles()).trim();
      });
      assert.equal(run.exportedSmiles, 'CCO');
      assert.ok(run.final.oxygenVisible && run.final.oxygenHit && run.final.buttonsReady);
    }
    (warming ? warmups : runs).push(run);
    console.log(JSON.stringify({ label, warming, from: run.from, target, mountMs: run.mountMs, idleMs: run.idleMs, atomsMs: run.atomsMs, readyMs: run.readyMs, afterAnimationMs: run.afterAnimationMs, firstWorkerMs: run.firstWorkerMs, workers: run.workersCreated, retainedRoot: run.retainedRoot }));
    await writeFile(output, JSON.stringify({ label, base, settings, browser: browser.version(), time: new Date().toISOString(), warmups, runs, network, pageErrors }, null, 2));
  }
  const ownerRuns = runs.filter(run => run.workersCreated);
  assert.equal(ownerRuns.length, 10);
  summary = Object.fromEntries(['afterAnimationMs', 'idleMs', 'interactiveMs'].map(key => [key, {
    p50: percentile(ownerRuns.map(run => run[key]), .5), p95: percentile(ownerRuns.map(run => run[key]), .95)
  }]));
  summary.workersStartedBeforeAnimationEnd = ownerRuns.filter(run => run.firstWorkerMs < run.idleMs).length;
  summary.singleWorkerPerMount = ownerRuns.every(run => run.workersCreated === 1);
  assert.deepEqual(pageErrors, []);
  if (acceptance) {
    assert.equal(summary.workersStartedBeforeAnimationEnd, 10);
    assert.ok(summary.singleWorkerPerMount);
    assert.equal(summary.afterAnimationMs.p50, 0);
    assert.ok(summary.afterAnimationMs.p95 <= 100, `Loading tail P95 ${summary.afterAnimationMs.p95}ms exceeds 100ms`);
  }
  const trace = await page.evaluate(() => ({ ...window.__loadProbe, resources: performance.getEntriesByType('resource').map(e => ({ name: e.name, start: e.startTime, duration: e.duration, transfer: e.transferSize, encoded: e.encodedBodySize, decoded: e.decodedBodySize })) }));
  await writeFile(output, JSON.stringify({ label, base, settings, browser: browser.version(), time: new Date().toISOString(), coldLoad, summary, warmups, runs, trace, network, pageErrors, passed: true }, null, 2));
  console.log(JSON.stringify({ label, summary, passed: true }));
} catch (error) {
  console.error(error);
  await writeFile(output, JSON.stringify({ label, base, settings, error: String(error), coldLoad, summary, warmups, runs, trace: await page.evaluate(() => window.__loadProbe).catch(() => null), network, pageErrors, passed: false }, null, 2));
  await page.screenshot({ path: resolve(outputDir, `${label}-failure.png`) }).catch(() => {});
  process.exitCode = 1;
} finally { await browser.close(); }

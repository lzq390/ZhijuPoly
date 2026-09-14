// Compare immutable source snapshots on two independent development servers.
// No routing interception: browser HTTP caches remain enabled for cold/warm runs.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
import { pollBrowser } from './browser-poll.mjs';

const output = resolve(process.env.REFRESH_OUTPUT || '/tmp/nexpoly-refresh-performance');
const rounds = Number(process.env.REFRESH_ROUNDS || 10);
const contextMode = process.env.REFRESH_CONTEXT || 'persistent';
assert.ok(['persistent', 'isolated'].includes(contextMode));
const routes = (process.env.REFRESH_ROUTES || '/structure-workbench,/database-query,/explorer,/homopolymer-property-prediction,/conditional-generation,/reverse-design').split(',');
const variants = {
  baseline: process.env.REFRESH_BASELINE_URL || 'http://127.0.0.1:5911',
  optimized: process.env.REFRESH_OPTIMIZED_URL || 'http://127.0.0.1:5912'
};
for (const base of Object.values(variants)) assert.equal(new URL(base).hostname, '127.0.0.1');
assert.ok(Number.isInteger(rounds) && rounds > 0);
await mkdir(output, { recursive: true });
const executablePath = process.env.STRUCTURE_CHROMIUM_PATH || undefined;
const samples = [], warmups = [];
const settings = { rounds, routes, variants, viewport: { width: 1440, height: 900 }, latencyMs: 40,
  startedAt: new Date().toISOString(),
  scriptSha256: createHash('sha256').update(await readFile(new URL(import.meta.url))).digest('hex'),
  downloadMbps: 50, uploadMbps: 10, reducedMotion: 'no-preference', acceptedEncoding: 'browser default; negotiated Content-Encoding retained in raw responses',
  cache: `fresh ${contextMode} context per variant/route/round; cold then ordinary reload then cache-disabled reload`,
  contextMode,
  ordering: 'baseline/optimized order reversed each round; all failures retained',
  ready: 'visible SDK drawing SVG, native ready, unlocked surface, common clear button enabled',
  validation: 'CCO import, actual oxygen drag, SMILES export after the timing window; no business jobs',
  noncriticalResources: ['Existing /favicon.ico 404 retained in requests but not a canvas resource failure'] };
let browserVersion;

async function launch() {
  const profile = await mkdtemp(resolve(output, 'profile-'));
  const options = { executablePath, headless: true, args: ['--no-sandbox', '--enable-unsafe-swiftshader'] };
  const browser = contextMode === 'isolated' ? await chromium.launch(options) : undefined;
  const context = browser ? await browser.newContext({ viewport: settings.viewport, reducedMotion: settings.reducedMotion })
    : await chromium.launchPersistentContext(profile, { ...options, viewport: settings.viewport, reducedMotion: settings.reducedMotion });
  browserVersion = context.browser().version();
  // Use a fresh tab after persistent-context startup.
  const page = await context.newPage();
  page.setDefaultTimeout(45000);
  await context.addInitScript(() => {
    const trace = window.__refresh = { events: [], longtasks: [], frames: [], readyMs: null, workers: 0 };
    const record = (event, extra = {}) => trace.events.push({ event, time: performance.now(), ...extra });
    const NativeWorker = window.Worker;
    window.Worker = new Proxy(NativeWorker, { construct(Target, args) {
      trace.workers++;
      record('worker-create');
      const worker = new Target(...args);
      let first = true;
      worker.addEventListener('message', () => { if (first) { first = false; record('worker-first-reply'); } });
      return worker;
    } });
    new PerformanceObserver(list => {
      for (const entry of list.getEntries()) if (!trace.readyMs || entry.startTime < trace.readyMs)
        trace.longtasks.push({ start: entry.startTime, duration: entry.duration });
    }).observe({ type: 'longtask', buffered: true });
    const seen = new Set();
    const once = (event, condition) => { if (condition && !seen.has(event)) { seen.add(event); record(event); } };
    let last;
    function sample() {
      const start = performance.now();
      const root = document.querySelector('[data-structure-editor]');
      const sdk = window.ketcher;
      const canvas = sdk?.editor?.render?.paper?.canvas;
      once('app', document.querySelector('[data-module-content]'));
      once('editor-mount', root);
      once('sdk', sdk);
      once('svg', canvas);
      const clear = document.querySelector('[data-workbench-tool="clear"]');
      if (root?.dataset.editorStatus === 'ready' && canvas && clear && !clear.disabled &&
          !root.closest('[hidden],[aria-hidden="true"],[inert]')) {
        const bounds = canvas.getBoundingClientRect();
        const container = root.getBoundingClientRect();
        if (bounds.width > 0 && bounds.height > 0 && container.width > 0 && container.height > 0) {
          trace.readyMs = performance.now(); record('ready');
        }
      }
      if (last !== undefined) trace.frames.push({ start, interval: start - last, probeMs: performance.now() - start });
      last = start;
      if (!trace.readyMs) requestAnimationFrame(sample);
    }
    requestAnimationFrame(sample);
  });
  const cdp = await context.newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: settings.latencyMs,
    downloadThroughput: settings.downloadMbps * 1e6 / 8, uploadThroughput: settings.uploadMbps * 1e6 / 8 });
  let requests = new Map(), errors = [];
  cdp.on('Network.requestWillBeSent', event => requests.set(event.requestId, {
    url: event.request.url, method: event.request.method, type: event.type, start: event.timestamp }));
  cdp.on('Network.responseReceived', event => {
    const item = requests.get(event.requestId);
    if (item) Object.assign(item, { status: event.response.status, headers: event.response.headers,
      diskCache: event.response.fromDiskCache || false, protocol: event.response.protocol });
  });
  cdp.on('Network.requestServedFromCache', event => {
    const item = requests.get(event.requestId); if (item) item.memoryCache = true;
  });
  cdp.on('Network.loadingFinished', event => {
    const item = requests.get(event.requestId); if (item) Object.assign(item, { wireBytes: event.encodedDataLength, end: event.timestamp });
  });
  cdp.on('Network.loadingFailed', event => {
    const item = requests.get(event.requestId); if (item) item.error = event.errorText;
  });
  page.on('pageerror', error => errors.push(error.message));

  async function measure(base, route, kind, variant, round, validate = true) {
    requests = new Map(); errors = [];
    const result = { variant, round, route, kind, base, passed: false };
    try {
      await cdp.send('Network.setCacheDisabled', { cacheDisabled: kind === 'uncached' });
      if (kind === 'cold' || kind === 'warmup') await page.goto(base + route, { waitUntil: 'domcontentloaded' });
      else await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => !!window.__refresh?.readyMs);
      const snapshot = await page.evaluate(() => ({ trace: window.__refresh,
        navigation: performance.getEntriesByType('navigation')[0].toJSON(),
        resources: performance.getEntriesByType('resource').map(r => ({ url: r.name, start: r.startTime,
          end: r.responseEnd, encoded: r.encodedBodySize, decoded: r.decodedBodySize, wire: r.transferSize })) }));
      Object.assign(result, snapshot, { requests: [...requests.values()].map(r => ({ ...r })), errors: [...errors] });
      const timings = Object.fromEntries(snapshot.trace.events.map(e => [e.event, e.time]));
      const sdkRequest = snapshot.resources.find(r => /\/KetcherReactRuntime[.-]/.test(r.url));
      const ownRequests = result.requests.filter(r => new URL(r.url).origin === new URL(base).origin);
      result.metrics = {
        appMs: timings.app, editorMountMs: timings['editor-mount'], sdkRequestMs: sdkRequest?.start ?? null,
        sdkMs: timings.sdk, svgMs: timings.svg, workerMs: timings['worker-create'],
        workerReplyMs: timings['worker-first-reply'], readyMs: snapshot.trace.readyMs,
        requestCount: ownRequests.length, sourceRequests: ownRequests.filter(r => new URL(r.url).pathname.startsWith('/src/')).length,
        wireBytes: ownRequests.reduce((n, r) => n + (r.wireBytes || 0), 0),
        workers: snapshot.trace.workers, longtaskTotalMs: snapshot.trace.longtasks.reduce((n, r) => n + r.duration, 0),
        longtaskMaxMs: Math.max(0, ...snapshot.trace.longtasks.map(r => r.duration)),
        maxProbeMs: Math.max(0, ...snapshot.trace.frames.map(r => r.probeMs))
      };
      assert.equal(snapshot.trace.workers, 1, 'Each refresh creates exactly one Worker');
      assert.deepEqual(errors, [], 'No unhandled page errors');
      assert.equal(ownRequests.filter(r => r.status >= 400 && !new URL(r.url).pathname.startsWith('/api/') &&
        new URL(r.url).pathname !== '/favicon.ico').length, 0, 'No failed frontend resources');
      if (validate) {
        // Real editing is outside the timing window and only uses local SDK APIs.
        // Let the existing post-ready centering finish before the mouse drag.
        await page.waitForTimeout(350);
        await page.evaluate(() => window.ketcher.setMolecule('CCO'));
        await page.waitForFunction(() => window.ketcher?.editor?.struct()?.atoms?.size === 3);
        await page.locator('[data-testid="select-rectangle"]:visible').click();
        const before = await page.evaluate(async () => {
          window.ketcher.editor.selection(null);
          const element = [...window.ketcher.editor.render.paper.canvas.querySelectorAll('text')].find(n => n.textContent === 'O');
          const box = element.getBoundingClientRect();
          const ket = JSON.parse(await window.ketcher.getKet());
          const molecule = Object.values(ket).find(v => v?.type === 'molecule');
          return { x: box.x + box.width / 2, y: box.y + box.height / 2, locations: molecule.atoms.map(a => a.location) };
        });
        await page.mouse.move(before.x, before.y);
        await page.mouse.down();
        await page.mouse.move(before.x + 50, before.y - 25, { steps: 5 });
        await page.mouse.up();
        await pollBrowser(page, async previous => {
          const ket = JSON.parse(await window.ketcher.getKet());
          const atoms = Object.values(ket).find(v => v?.type === 'molecule').atoms;
          return JSON.stringify(atoms.map(a => a.location)) !== JSON.stringify(previous);
        }, before.locations);
        result.exportedSmiles = await page.evaluate(() => window.ketcher.getSmiles());
        assert.equal(result.exportedSmiles, 'CCO');
        result.realAtomDrag = true;
      }
      result.passed = true;
    } catch (error) {
      result.error = String(error.stack || error);
      result.requests ||= [...requests.values()]; result.errors ||= [...errors];
      try {
        result.failureState = await page.evaluate(() => ({ trace: window.__refresh, text: document.body.innerText.slice(-2000),
          editor: document.querySelector('[data-structure-editor]')?.dataset,
          rectangles: [...document.querySelectorAll('[data-structure-editor],.np-structure-native')].map(e => ({ cls: e.className, bounds: e.getBoundingClientRect().toJSON() })) }));
        await page.screenshot({ path: resolve(output, `failure-${variant}-${round}-${route.slice(1)}-${kind}.png`) });
      } catch {}
    }
    return result;
  }
  return { measure, close: async () => {
    await context.close();
    await browser?.close();
    // Chromium may finish its profile flush just after closing the context.
    await rm(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  } };
}

async function save() {
  await writeFile(resolve(output, 'samples.json'), JSON.stringify({ settings, browserVersion, warmups, samples }, null, 2));
}

// Predeclare one full server-transform warmup. Browser-cold samples still use new profiles.
for (const [variant, base] of Object.entries(variants)) {
  const runner = await launch();
  try { for (const route of routes) warmups.push(await runner.measure(base, route, 'warmup', variant, 0, false)); }
  finally { await runner.close(); }
  await save();
  console.log(JSON.stringify({ warmup: variant, failures: warmups.filter(r => !r.passed).length }));
}
for (let round = 1; round <= rounds; round++) for (const route of routes) {
  for (const variant of round % 2 ? ['baseline', 'optimized'] : ['optimized', 'baseline']) {
    const runner = await launch();
    try {
      for (const kind of ['cold', 'warm', 'uncached']) {
        const result = await runner.measure(variants[variant], route, kind, variant, round);
        samples.push(result); await save();
        console.log(JSON.stringify({ variant, round, route, kind, passed: result.passed, ...result.metrics, error: result.error?.split('\n')[0] }));
      }
    } finally { await runner.close(); }
  }
}
const quantile = (values, q) => [...values].sort((a, b) => a - b)[Math.ceil(values.length * q) - 1] ?? null;
const comparisons = routes.map(route => {
  const groups = Object.fromEntries(Object.keys(variants).map(variant => [variant,
    Object.fromEntries(['cold', 'warm', 'uncached'].map(kind => {
      const group = samples.filter(s => s.route === route && s.variant === variant && s.kind === kind);
      return [kind, { count: group.length, failures: group.filter(s => !s.passed).length,
        ...Object.fromEntries(['readyMs', 'wireBytes', 'sourceRequests', 'sdkRequestMs', 'longtaskMaxMs'].map(metric => [metric,
          { p50: quantile(group.map(s => s.metrics?.[metric] ?? Infinity), .5), p95: quantile(group.map(s => s.metrics?.[metric] ?? Infinity), .95) }])) }];
    }))]));
  const checks = {
    samplesComplete: Object.values(groups).every(g => Object.values(g).every(s => s.count === rounds && s.failures === 0)),
    warmMedian30Percent: groups.optimized.warm.readyMs.p50 <= groups.baseline.warm.readyMs.p50 * .7,
    warmP95: groups.optimized.warm.readyMs.p95 <= groups.baseline.warm.readyMs.p95,
    coldBytes50Percent: groups.optimized.cold.wireBytes.p50 <= groups.baseline.cold.wireBytes.p50 * .5
  };
  return { route, ...groups, checks, passed: Object.values(checks).every(Boolean) };
});
const report = { settings, browserVersion, comparisons, totalSamples: samples.length,
  failures: samples.filter(s => !s.passed).map(({ variant, round, route, kind, error }) => ({ variant, round, route, kind, error })),
  passed: warmups.every(s => s.passed) && comparisons.every(c => c.passed) };
await writeFile(resolve(output, 'result.json'), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
if (!report.passed) process.exitCode = 1;

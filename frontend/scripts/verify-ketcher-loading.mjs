// Full-application comparison. No request routing: preserve real HTTP/code caches.
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, writeFile, appendFile, rm, access } from 'node:fs/promises';
import { cpus, loadavg, totalmem, release as kernelRelease } from 'node:os';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';

const output = resolve(process.env.KETCHER_LOADING_OUTPUT || '/tmp/nexpoly-ketcher-loading');
const rounds = Number(process.env.KETCHER_LOADING_ROUNDS || 30);
const routes = (process.env.KETCHER_LOADING_ROUTES || '/structure-workbench,/database-query,/explorer,/homopolymer-property-prediction,/conditional-generation,/reverse-design').split(',');
const networks = (process.env.KETCHER_LOADING_NETWORKS || 'local,remote').split(',');
const modes = (process.env.KETCHER_LOADING_MODES || 'cold,warm,uncached').split(',');
const contextMode = process.env.KETCHER_LOADING_CONTEXT || 'persistent';
assert.ok(['persistent', 'isolated'].includes(contextMode));
const canvasSize = process.env.KETCHER_LOADING_CANVAS_SIZE === 'natural' ? null :
  Object.fromEntries((process.env.KETCHER_LOADING_CANVAS_SIZE || '878x566').split('x').map((value, index) => [['width', 'height'][index], Number(value)]));
if (canvasSize) assert.ok(canvasSize.width >= 600 && canvasSize.height >= 400);
const variants = [
  { name: process.env.KETCHER_LOADING_REFERENCE_NAME || 'iframe', base: process.env.KETCHER_LOADING_IFRAME || 'http://127.0.0.1:9000' },
  { name: process.env.KETCHER_LOADING_CANDIDATE_NAME || 'native', base: process.env.KETCHER_LOADING_NATIVE || 'http://127.0.0.1:5962' }
];
const [referenceName, candidateName] = variants.map(variant => variant.name);
assert.ok(Number.isInteger(rounds) && rounds > 0);
assert.notEqual(referenceName, candidateName);
for (const value of variants) {
  assert.equal(new URL(value.base).hostname, '127.0.0.1');
  assert.match(value.name, /^[a-z0-9-]+$/);
}
await mkdir(output, { recursive: true });
await access(resolve(output, 'samples.jsonl')).then(() => { throw new Error('Use a fresh output directory; existing raw samples must not be overwritten.'); }, error => { if (error.code !== 'ENOENT') throw error; });
const settings = { routes, networks, modes, rounds, variants, contextMode, device: { cpu: cpus()[0]?.model, logicalCpus: cpus().length, memoryBytes: totalmem(), kernel: kernelRelease() }, viewport: { width: 1440, height: 900 }, canvasSize,
  canvasContainerInsets: { iframe: { width: 104, height: 78 }, native: { width: 94, height: 72 } },
  reducedMotion: 'no-preference', remote: { latency: 40, downloadMbps: 50, uploadMbps: 10,
    acceptedEncodings: ['gzip', 'deflate'], encodingReason: 'Matches Chromium requests to the actual HTTP 9001 address; loopback otherwise also advertises Brotli/Zstd.' },
  startedAt: new Date().toISOString(), quantiles: 'nearest rank',
  order: 'reverse variant order each round; cold/warm/uncached share one fresh persistent profile',
  cache: `fresh ${contextMode} context per variant/route/round; warm normal reload; uncached HTTP cache disabled (process/code cache retained)`,
  transfer: 'Completed HTTP resources at the readiness snapshot; same-origin and all-origin totals reported separately. Missing external Worker completion uses ResourceTiming.transferSize (browser header estimate). In-flight/background transfers are not the complete navigation total.',
  scriptSha256: createHash('sha256').update(await readFile(new URL(import.meta.url))).digest('hex') };
await writeFile(resolve(output, 'harness.mjs'), await readFile(new URL(import.meta.url)));
const samples = [], warmups = [];
function probe({ targetCanvas, insets }) {
  const trace = window.__loading = { events: [], longtasks: [], workers: 0, activeWorkers: 0 };
  const record = (event, extra = {}) => trace.events.push({ event, at: performance.timeOrigin + performance.now(), ...extra });
  if (window === top && targetCanvas) {
    const install = () => {
      const style = document.createElement('style');
      style.dataset.ketcherMeasurement = '';
      style.textContent = `iframe[src*="ketcher"]{width:${targetCanvas.width + insets.iframe.width}px!important;height:${targetCanvas.height + insets.iframe.height}px!important}` +
        `.np-structure-native{width:${targetCanvas.width + insets.native.width}px!important;height:${targetCanvas.height + insets.native.height}px!important}`;
      document.head.append(style);
      record('canvas-preset');
    };
    // Set the container before SDK construction. Resizing after construction
    // would introduce additional observer work into the measured startup.
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install, { once: true });
    else install();
  }
  const Worker0 = window.Worker;
  window.Worker = new Proxy(Worker0, { construct(Target, args) {
    ++trace.workers; ++trace.activeWorkers;
    record('worker-create', { url: String(args[0]) });
    const worker = new Target(...args);
    const terminate = worker.terminate.bind(worker);
    let retired = false, first = true;
    worker.terminate = () => { if (!retired) { retired = true; --trace.activeWorkers; record('worker-retire'); } terminate(); };
    worker.addEventListener('message', () => { if (first) { first = false; record('worker-reply'); } });
    return worker;
  } });
  new PerformanceObserver(list => { for (const entry of list.getEntries()) trace.longtasks.push({ start: entry.startTime, duration: entry.duration }); }).observe({ type: 'longtask', buffered: true });
  window.__loadingSdk = () => {
    for (const frame of document.querySelectorAll('iframe')) {
      try { if (frame.contentWindow?.ketcher) return frame.contentWindow.ketcher; } catch { /* Cross origin application panel. */ }
    }
    return window.ketcher;
  };
  const seen = new Set();
  const once = (event, value) => { if (value && !seen.has(event)) { seen.add(event); record(event); } };
  function matchCanvas(canvas) {
    if (!targetCanvas) return true;
    const bounds = canvas.getBoundingClientRect();
    return Math.abs(bounds.width - targetCanvas.width) < .5 && Math.abs(bounds.height - targetCanvas.height) < .5;
  }
  function sample() {
    const sdk = window.__loadingSdk(), canvas = sdk?.editor?.render?.paper?.canvas;
    const root = document.querySelector('[data-structure-editor]');
    const clear = document.querySelector('[data-workbench-tool="clear"]');
    once('app', document.querySelector('[data-module-content]'));
    once('sdk', sdk); once('svg', canvas);
    if (canvas && !seen.has('visible-svg') && !root?.closest('[hidden],[aria-hidden="true"]')) {
      const bounds = canvas.getBoundingClientRect();
      once('visible-svg', bounds.width > 0 && bounds.height > 0);
    }
    const fixedCanvas = window === top && canvas && matchCanvas(canvas);
    if (window === top && canvas && fixedCanvas && clear && !clear.disabled &&
        (!root?.dataset.editorStatus || root.dataset.editorStatus === 'ready') &&
        !root?.closest('[hidden],[inert],[aria-hidden="true"]')) {
      const bounds = canvas.getBoundingClientRect();
      if (bounds.width > 0 && bounds.height > 0) {
        trace.uiReadyMs = performance.now();
        trace.canvas = { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height };
        Promise.resolve().then(() => sdk.getSmiles()).then(value => {
          trace.initialSmiles = value; trace.readyMs = performance.now();
        }, error => { trace.exportError = String(error); });
        return;
      }
    }
    requestAnimationFrame(sample);
  }
  requestAnimationFrame(sample);
}
async function contextFor() {
  const launch = {
    executablePath: process.env.STRUCTURE_CHROMIUM_PATH || undefined, headless: true,
    args: ['--no-sandbox', '--enable-unsafe-swiftshader']
  };
  const options = { viewport: settings.viewport, reducedMotion: settings.reducedMotion };
  const profile = contextMode === 'persistent' ? await mkdtemp(resolve(output, 'profile-')) : undefined;
  const browser = profile ? undefined : await chromium.launch(launch);
  const context = profile ? await chromium.launchPersistentContext(profile, { ...launch, ...options }) : await browser.newContext(options);
  settings.browser = context.browser().version();
  await context.addInitScript(probe, { targetCanvas: canvasSize, insets: settings.canvasContainerInsets });
  return { context, profile, browser };
}
async function validate(page) {
  await page.evaluate(() => window.__loadingSdk().setMolecule('CCO'));
  await page.waitForFunction(() => window.__loadingSdk().editor.struct().atoms.size === 3);
  const embedded = await page.locator('iframe[src*="ketcher"]').count();
  const ui = embedded ? page.frameLocator('iframe[src*="ketcher"]') : page;
  await ui.locator('[data-testid="select-rectangle"]:visible').click();
  const before = await page.evaluate(async () => {
    const sdk = window.__loadingSdk(); sdk.editor.selection(null);
    const oxygen = [...sdk.editor.render.paper.canvas.querySelectorAll('text')].find(node => node.textContent === 'O');
    if (!oxygen) throw new Error('Visible oxygen was not rendered');
    const atom = oxygen.getBoundingClientRect();
    const frame = document.querySelector('iframe[src*="ketcher"]'), bounds = frame?.getBoundingClientRect();
    const scale = frame ? bounds.width / frame.contentWindow.innerWidth : 1;
    const molecule = Object.values(JSON.parse(await sdk.getKet())).find(value => value?.type === 'molecule');
    return { x: (bounds?.x || 0) + (atom.x + atom.width / 2) * scale,
      y: (bounds?.y || 0) + (atom.y + atom.height / 2) * scale, coordinates: molecule.atoms.map(atom => atom.location) };
  });
  await page.mouse.move(before.x, before.y); await page.mouse.down();
  await page.mouse.move(before.x + 40, before.y - 20, { steps: 5 }); await page.mouse.up();
  await page.waitForFunction(async old => {
    const molecule = Object.values(JSON.parse(await window.__loadingSdk().getKet())).find(value => value?.type === 'molecule');
    return JSON.stringify(molecule.atoms.map(atom => atom.location)) !== JSON.stringify(old);
  }, before.coordinates);
  assert.equal(await page.evaluate(() => window.__loadingSdk().getSmiles()), 'CCO');
  await page.locator('[data-workbench-tool="clear"]').click();
  await page.waitForFunction(() => window.__loadingSdk().editor.struct().atoms.size === 0 && !document.querySelector('[data-workbench-tool="clear"]').disabled);
  // Await actual paint/operation completion instead of sleeping through fixed 700ms.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await page.evaluate(() => window.__loadingSdk().getSmiles()), '');
  return { imported: 'CCO', actualAtomDrag: true, exported: 'CCO', cleared: true };
}
const json = value => JSON.stringify(value, (_key, item) => typeof item === 'number' && !Number.isFinite(item) ? String(item) : item, 2);
async function save() {
  await writeFile(resolve(output, 'progress.json'), json({ settings, completed: samples.length, warmups: warmups.length, last: samples.at(-1) }));
}
for (const network of networks) for (const route of routes) for (let round = 1; round <= rounds; ++round) {
  for (const variant of round % 2 ? variants : [...variants].reverse()) {
    const { context, profile, browser } = await contextFor();
    if (network === 'remote') await context.setExtraHTTPHeaders({ 'Accept-Encoding': settings.remote.acceptedEncodings.join(', ') });
    const page = await context.newPage(); page.setDefaultTimeout(30000);
    const cdp = await context.newCDPSession(page); await cdp.send('Network.enable');
    if (network === 'remote') {
      await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: 40, downloadThroughput: 50e6 / 8, uploadThroughput: 10e6 / 8 });
    }
    let requests = new Map(), errors = [];
    cdp.on('Network.requestWillBeSent', e => requests.set(e.requestId, { url: e.request.url, frameId: e.frameId, type: e.type, start: e.timestamp }));
    cdp.on('Network.responseReceived', e => { const item = requests.get(e.requestId); if (item) Object.assign(item, { status: e.response.status, headers: e.response.headers, diskCache: e.response.fromDiskCache }); });
    cdp.on('Network.requestServedFromCache', e => { const item = requests.get(e.requestId); if (item) item.memoryCache = true; });
    cdp.on('Network.loadingFinished', e => { const item = requests.get(e.requestId); if (item) Object.assign(item, { wireBytes: e.encodedDataLength, end: e.timestamp }); });
    cdp.on('Network.loadingFailed', e => { const item = requests.get(e.requestId); if (item) item.failure = e.errorText; });
    page.on('pageerror', error => errors.push(String(error)));
    let visited = false;
    async function measure(mode, warmup = false) {
      requests = new Map(); errors = [];
      const sample = { network, route, round, variant: variant.name, mode, passed: false, hostLoadAtStart: loadavg() };
      try {
        await cdp.send('Network.setCacheDisabled', { cacheDisabled: mode === 'uncached' });
        if (!visited) { visited = true; await page.goto(variant.base + route, { waitUntil: 'domcontentloaded' }); }
        else await page.reload({ waitUntil: 'domcontentloaded' });
        await page.waitForFunction(() => window.__loading?.readyMs || window.__loading?.exportError);
        const frames = [];
        for (const frame of page.frames()) {
          if (!frame.url().startsWith(variant.base)) continue;
          frames.push(await frame.evaluate(() => ({ url: location.href, epoch: performance.timeOrigin, trace: window.__loading,
            resources: performance.getEntriesByType('resource').map(entry => entry.toJSON()),
            marks: performance.getEntriesByType('mark').map(entry => entry.toJSON()) })));
        }
        const root = frames[0]; sample.frames = frames;
        // Network events continue during the later drawing validation. Freeze
        // this snapshot so those events cannot mutate the recorded evidence.
        sample.requests = [...requests.values()].map(request => ({ ...request, headers: request.headers ? { ...request.headers } : undefined }));
        sample.errors = [...errors];
        assert.ok(!root.trace.exportError, root.trace.exportError);
        assert.equal(root.trace.initialSmiles, '');
        const events = frames.flatMap(frame => frame.trace?.events || []);
        const first = name => events.filter(event => event.event === name).sort((a, b) => a.at - b.at)[0]?.at - root.epoch;
        const owned = sample.requests.filter(item => item.url.startsWith(variant.base));
        const http = sample.requests.filter(item => /^https?:/.test(item.url));
        const supplements = frames.flatMap(frame => frame.resources).filter(resource => resource.name.startsWith(variant.base) &&
          owned.some(request => request.url === resource.name && request.wireBytes === undefined));
        const allSupplements = frames.flatMap(frame => frame.resources).filter(resource =>
          http.some(request => request.url === resource.name && request.wireBytes === undefined));
        const sdkResources = frames.flatMap(frame => frame.resources.map(resource => ({ ...resource, epoch: frame.epoch })))
          .filter(resource => /\/assets\/ketcher\/|\/ketcher\/(?:index\.html|static\/js\/)/.test(resource.name));
        const longtasks = root.trace.longtasks.filter(task => task.start < root.trace.readyMs);
        sample.metrics = { uiReadyMs: root.trace.uiReadyMs, readyMs: root.trace.readyMs,
          firstExportMs: root.trace.readyMs - root.trace.uiReadyMs, appMs: first('app'), svgMs: first('svg'), visibleSvgMs: first('visible-svg'),
          sdkRequestMs: Math.min(...sdkResources.map(resource => resource.epoch + resource.startTime - root.epoch)),
          workerMs: first('worker-create'), workerReplyMs: first('worker-reply'),
          workerClaimMs: root.marks.find(mark => mark.name === 'ketcher:worker-claimed')?.startTime,
          workers: frames.reduce((sum, frame) => sum + (frame.trace?.workers || 0), 0),
          requests: owned.length, wireBytes: owned.reduce((sum, item) => sum + (item.wireBytes || 0), 0) + supplements.reduce((sum, item) => sum + item.transferSize, 0),
          totalRequests: http.length,
          totalWireBytes: http.reduce((sum, item) => sum + (item.wireBytes || 0), 0) + allSupplements.reduce((sum, item) => sum + item.transferSize, 0),
          workerTransferSupplements: supplements.map(item => ({ url: item.name, bytes: item.transferSize })),
          longtaskMaxMs: Math.max(0, ...longtasks.map(task => task.duration)),
          longtaskTotalMs: longtasks.reduce((sum, task) => sum + task.duration, 0) };
        assert.equal(sample.metrics.workers, 1);
        assert.equal(sample.errors.length, 0, sample.errors.join('\n'));
        sample.validation = await validate(page);
        sample.errors = [...errors];
        assert.equal(sample.errors.length, 0, sample.errors.join('\n'));
        sample.passed = true;
      } catch (error) {
        sample.error = String(error.stack || error);
        sample.requests ||= [...requests.values()]; sample.errors ||= [...errors];
        await page.screenshot({ path: resolve(output, `failure-${network}-${route.slice(1)}-${round}-${variant.name}-${mode}.png`) }).catch(() => {});
      }
      sample.id = `${network}-${route.slice(1)}-${round}-${variant.name}-${mode}`;
      await appendFile(resolve(output, 'samples.jsonl'), JSON.stringify(sample) + '\n');
      const { frames, requests: evidenceRequests, ...compact } = sample;
      (warmup ? warmups : samples).push(compact);
      await save();
      console.log(JSON.stringify({ network, route, round, variant: variant.name, mode, warmup, passed: sample.passed,
        uiReadyMs: sample.metrics?.uiReadyMs, readyMs: sample.metrics?.readyMs, wireBytes: sample.metrics?.wireBytes, error: sample.error?.split('\n')[0] }));
    }
    try {
      if (!modes.includes('cold')) await measure('cold', true);
      for (const mode of modes) await measure(mode);
    } finally {
      await context.close(); await browser?.close();
      if (profile) await rm(profile, { recursive: true, force: true });
    }
  }
}
const q = (rows, field, percentile) => {
  const values = rows.map(row => row.passed ? row.metrics[field] : Infinity).sort((a, b) => a - b);
  return values[Math.ceil(values.length * percentile) - 1];
};
function confidence(differences) {
  let state = 3811; const random = () => (state = (Math.imul(state, 1664525) + 1013904223) >>> 0) / 2 ** 32;
  if (differences.some(value => !Number.isFinite(value))) return null;
  const values = Array.from({ length: 2000 }, () => {
    const sample = differences.map(() => differences[Math.floor(random() * differences.length)]).sort((a, b) => a - b);
    return sample[Math.ceil(sample.length / 2) - 1];
  }).sort((a, b) => a - b);
  return [values[49], values[1949]];
}
const comparisons = [];
for (const network of networks) for (const route of routes) for (const mode of modes) {
  const rows = variant => samples.filter(row => row.network === network && row.route === route && row.mode === mode && row.variant === variant);
  const old = rows(referenceName), native = rows(candidateName);
  const comparison = { network, route, mode, count: native.length,
    failures: { [referenceName]: old.filter(row => !row.passed).length, [candidateName]: native.filter(row => !row.passed).length }, metrics: {} };
  for (const field of ['uiReadyMs', 'readyMs', 'wireBytes', 'totalWireBytes']) {
    const differences = native.map((row, index) => row.passed && old[index]?.passed ? row.metrics[field] - old[index].metrics[field] : Infinity);
    comparison.metrics[field] = { [referenceName]: { p50: q(old, field, .5), p95: q(old, field, .95) },
      [candidateName]: { p50: q(native, field, .5), p95: q(native, field, .95) },
      pairedMedian: [...differences].sort((a, b) => a - b)[Math.ceil(differences.length / 2) - 1], pairedMedian95CI: confidence(differences) };
  }
  comparison.passed = comparison.failures[referenceName] === 0 && comparison.failures[candidateName] === 0 && native.length === rounds &&
    ['uiReadyMs', 'readyMs'].every(field => ['p50', 'p95'].every(percentile => comparison.metrics[field][candidateName][percentile] <= comparison.metrics[field][referenceName][percentile]));
  comparisons.push(comparison);
}
await writeFile(resolve(output, 'results.json'), json({ settings, evidence: 'samples.jsonl', warmups, samples }));
const summary = { settings, passed: comparisons.every(value => value.passed), comparisons };
await writeFile(resolve(output, 'summary.json'), json(summary));
console.log(json(summary));
if (!summary.passed) process.exitCode = 1;
